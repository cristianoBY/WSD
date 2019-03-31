import sys
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D  

from torch.nn import Parameter
from torch.nn import MSELoss, L1Loss, SmoothL1Loss, CrossEntropyLoss
from torch.distributions.binomial import Binomial
import torch.nn.utils.rnn as rnn_utils

import pandas as pd
import numpy as np
import math
from scipy.stats import spearmanr, pearsonr
from sklearn.metrics import precision_score, f1_score, recall_score

from tqdm import tqdm
from tqdm import tqdm_notebook as tqdm_n

from collections import Iterable, defaultdict
import itertools

from allennlp.modules.elmo import Elmo, batch_to_ids
from allennlp.commands.elmo import ElmoEmbedder

class Trainer(object):

	def __init__(self, 
				 optimizer_class = torch.optim.Adam,
				 optim_wt_decay=0.,
				 epochs=3,
				 train_batch_size = 64,
				 data_name = None,
				 pretrain_data_name = None,
				 predict_batch_size = 128,
				 pretraining=False,
				 regularization = None,
				 file_path = "",
				device = torch.device(type="cpu"),
				**kwargs):

		## Training parameters
		self.epochs = epochs
		self.train_batch_size = train_batch_size
		self.predict_batch_size = predict_batch_size
		self.pretraining = pretraining
		self.data_name = data_name
		self.pretrain_data_name = pretrain_data_name

		## optimizer 
		self._optimizer_class = optimizer_class
		self.optim_wt_decay = optim_wt_decay
		
		self._init_kwargs = kwargs
		self.device = device
		
		if regularization == "l1":
			self.regularization = L1Loss()
		elif regularization == "smoothl1":
			self.regularization = SmoothL1Loss()
		else:
			self.regularization = None

		## save to Model file
		self.best_model_file =  file_path + "wsd_model_" + data_name + \
									"_" + str(optim_wt_decay) + \
									"_" + "pre_" + str(pretrain_data_name) + \
									"_" + str(regularization) + "_.pth"

		self.smooth_loss = SmoothL1Loss().to(self.device)
		self.l1_loss = L1Loss().to(self.device)

		if self.regularization:
			self.regularization = self.regularization.to(self.device)

	def _initialize_trainer_model(self):
		self._model = Model(device = self.device, **self._init_kwargs)
		self._model = self._model.to(self.device)

	def _custom_loss(self, predicted, actual, pretrain_x, pretrain_actual):
		'''
		Inputs:
		1. predicted: model predicted values
		2. actual: actual values
		'''
		actual_torch = torch.from_numpy(np.array(actual)).float().to(self.device)

		domain_loss = self.smooth_loss(predicted.squeeze(), actual_torch)
		return domain_loss

	
	def train(self, train_X, train_Y, dev, pretrain_x, pretrain_actual, **kwargs):

		self._X,  self._Y = train_X, train_Y
		
		self.pretrain_x = pretrain_x
		self.pretrain_actual = pretrain_actual

		if self.data_name != "megaverid":
			dev_x, dev_y = dev
			
		self._initialize_trainer_model()  

		parameters = [p for p in self._model.parameters() if p.requires_grad]
		optimizer = self._optimizer_class(parameters, weight_decay = self.optim_wt_decay, **kwargs)
		
		total_obs = len(self._X)
		#dev_obs = len(dev_x)
		
		#dev_accs = []
		best_loss = float('inf')
		best_r = -float('inf')
		train_losses = []
		dev_losses = []
		dev_rs = []
		bad_count = 0
		
		for epoch in range(self.epochs):
			
			batch_losses = []
			# Turn on training mode which enables dropout.
			self._model.train()
			
			bidx_i = 0
			bidx_j =self.train_batch_size
			
			tqdm.write("Running Epoch: {}".format(epoch+1))
			
			#time print
			pbar = tqdm_n(total = total_obs//self.train_batch_size)
			
			while bidx_j < total_obs:
				words = [words for words, spans in self._X[bidx_i:bidx_j]]
				spans = [spans for words, spans in self._X[bidx_i:bidx_j]]
				
				##Zero grad
				optimizer.zero_grad()

				##Calculate Loss
				model_out  = self._model(words, spans)   
				
				if self.pretraining:
					curr_loss = self._custom_loss(model_out, self._Y[bidx_i:bidx_j], pretrain_x, pretrain_actual)
				else:
					curr_loss = self._custom_loss(model_out, self._Y[bidx_i:bidx_j], None, None)
					
				batch_losses.append(curr_loss.detach().item())
				
				##Backpropagate
				curr_loss.backward()

				#plot_grad_flow(self._model.named_parameters())
				optimizer.step()
				bidx_i = bidx_j
				bidx_j = bidx_i + self.train_batch_size
				
				if bidx_j >= total_obs:
					words = [words for words, spans in self._X[bidx_i:bidx_j]]
					spans = [spans for words, spans in self._X[bidx_i:bidx_j]]
					##Zero grad
					optimizer.zero_grad()

					##Calculate Loss
					model_out  = self._model(words, spans)   
					
					if self.pretraining:
						curr_loss = self._custom_loss(model_out, self._Y[bidx_i:bidx_j], pretrain_x, pretrain_actual)
					else:
						curr_loss = self._custom_loss(model_out, self._Y[bidx_i:bidx_j], None, None)
					
					batch_losses.append(curr_loss.detach().item())
					##Backpropagate
					curr_loss.backward()

					#plot_grad_flow(self.named_parameters())
					optimizer.step()
					
				pbar.update(1)
					
			pbar.close()
			
			#print(batch_losses)
			curr_train_loss = np.mean(batch_losses)
			print("Epoch: {}, Mean Train Loss across batches: {}".format(epoch+1, curr_train_loss))
			
			if self.data_name == "megaverid":
				if curr_train_loss < best_loss:
					with open(self.best_model_file, 'wb') as f:
						torch.save(self._model.state_dict(), f)
					best_loss = curr_train_loss
				
				## Stop training when loss converges
				if epoch:
					if (abs(curr_train_loss - train_losses[-1]) < 0.0001):
						break

				train_losses.append(curr_train_loss)

			else:
				curr_dev_loss, curr_dev_preds = self.predict(dev_x, dev_y)
				curr_dev_r = pearsonr(curr_dev_preds.cpu().numpy(), dev_y)
				print("Epoch: {}, Mean Dev Loss across batches: {}, pearsonr: {}".format(epoch+1, 
																						curr_dev_loss,
																						curr_dev_r[0]))
				
				# if curr_dev_loss < best_loss:
				#     with open(self.best_model_file, 'wb') as f:
				#         torch.save(self._model.state_dict(), f)
				#     best_loss = curr_dev_loss


				if curr_dev_r[0] > best_r:
					with open(self.best_model_file, 'wb') as f:
						torch.save(self._model.state_dict(), f)
					best_r = curr_dev_r[0]
			

				# if epoch:
				#     if curr_dev_loss > dev_losses[-1]:
				#         bad_count+=1
				#     else:
				#         bad_count=0

				if epoch:
					if curr_dev_r[0] < dev_rs[-1]:
						bad_count+=1
					else:
						bad_count=0

				if bad_count >=3:
					break

				dev_rs.append(curr_dev_r[0])
				dev_losses.append(curr_dev_loss)
				train_losses.append(curr_train_loss)
			

		# print("Epoch: {}, Converging-Loss: {}".format(epoch+1, curr_mean_loss))

		return train_losses, dev_losses, dev_rs

	def predict_grad(self, data_x):
		'''
		Predictions with gradients and computation graph intact
		'''     
		bidx_i = 0
		bidx_j = self.predict_batch_size
		total_obs = len(data_x)
		yhat = torch.zeros(total_obs).to(self.device)

		while bidx_j < total_obs:
			words = [words for words, spans in data_x[bidx_i:bidx_j]]
			spans = [spans for words, spans in data_x[bidx_i:bidx_j]]
		
			##Calculate Loss
			model_out  = self._model(words, spans)   
			yhat[bidx_i:bidx_j] = model_out.squeeze()
			
			bidx_i = bidx_j
			bidx_j = bidx_i + self.train_batch_size
			
			if bidx_j >= total_obs:
				words = [words for words, spans in data_x[bidx_i:bidx_j]]
				spans = [spans for words, spans in data_x[bidx_i:bidx_j]]
				
				##Calculate Loss
				model_out  = self._model(words, spans)   
				yhat[bidx_i:bidx_j] = model_out.squeeze()
				
		return yhat

	def predict(self, data_x, data_y, loss=None):
		'''
		Predict loss, and prediction values for whole data_x
		'''
		# Turn on evaluation mode which disables dropout.
		self._model.eval()
		batch_losses = []
		
		with torch.no_grad():  
			bidx_i = 0
			bidx_j = self.predict_batch_size
			total_obs = len(data_x)
			yhat = torch.zeros(total_obs).to(self.device)

			while bidx_j < total_obs:
				words = [words for words, spans in data_x[bidx_i:bidx_j]]
				spans = [spans for words, spans in data_x[bidx_i:bidx_j]]
			
				##Calculate Loss
				model_out  = self._model(words, spans)   
				yhat[bidx_i:bidx_j] = model_out.squeeze()

				if self.pretraining:
					curr_loss = self._custom_loss(model_out, data_y[bidx_i:bidx_j], self.pretrain_x, self.pretrain_actual)
				else:
					if loss=="l1":
						actual_torch = torch.from_numpy(np.array(data_y[bidx_i:bidx_j])).float().to(self.device)
						curr_loss = self.l1_loss(model_out.squeeze(), actual_torch)
					else:
						curr_loss = self._custom_loss(model_out, data_y[bidx_i:bidx_j], None, None)

				batch_losses.append(curr_loss.detach().item())
				
				bidx_i = bidx_j
				bidx_j = bidx_i + self.train_batch_size
				
				if bidx_j >= total_obs:
					words = [words for words, spans in data_x[bidx_i:bidx_j]]
					spans = [spans for words, spans in data_x[bidx_i:bidx_j]]
					
					##Calculate Loss
					model_out  = self._model(words, spans)   
					yhat[bidx_i:bidx_j] = model_out.squeeze()
					if self.pretraining:
						curr_loss = self._custom_loss(model_out, data_y[bidx_i:bidx_j], self.pretrain_x, self.pretrain_actual)
					else:
						if loss=="l1":
							actual_torch = torch.from_numpy(np.array(data_y[bidx_i:bidx_j])).float().to(self.device)
							curr_loss = self.l1_loss(model_out.squeeze(), actual_torch)
						else:
							curr_loss = self._custom_loss(model_out, data_y[bidx_i:bidx_j], None, None)
					batch_losses.append(curr_loss.detach().item())
				

		return np.mean(batch_losses), yhat.detach()