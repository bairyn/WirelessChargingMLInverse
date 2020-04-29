# -*- coding: utf-8 -*-
# vim: set noet ft=python :

"""
Module providing methods to interface with the neural networks provided by this
package.
"""

import pandas as pd
import math
import numpy as np
import os
import shutil
import string
import torch
import torch.nn as nn

from wcmi.exception import WCMIError
from wcmi.log import logger

import wcmi.nn as wnn
import wcmi.nn.data as data
import wcmi.nn.dense as dense
import wcmi.nn.gan as gan
import wcmi.simulation as simulation

def train(
	use_gan=True, load_model_path=None, save_model_path=None,
	load_data_path=None, save_data_path=None, gan_n=gan.default_gan_n,
	num_epochs=data.default_num_epochs,
	status_every_epoch=data.default_status_every_epoch,
	status_every_sample=data.default_status_every_sample,
	batch_size=data.default_batch_size,
	learning_rate=data.default_learning_rate,
	gan_force_fixed_gen_params=False,
	gan_enable_pause=data.default_gan_enable_pause,
	gan_training_pause_threshold=data.default_gan_training_pause_threshold,
	pause_min_samples_per_epoch=data.default_pause_min_samples_per_epoch,
	pause_min_epochs=data.default_pause_min_epochs,
	pause_max_epochs=data.default_pause_max_epochs,
	logger=logger,
):
	# TODO: also output linear regression for each column for all_data
	# (predicted_out1 = b0 + b1*input1 + b2*input2 + ...)
	# (predicted_out2 = c0 + c1*input1 + c2*input2 + ...)
	# (...)
	"""
	Train a neural network with data and save it.
	"""

	# Default arguments.
	if gan_n is None:
		gan_n = gan.default_gan_n

	# Argument verification.
	if load_data_path is None:
		raise WCMIError("error: train requires --load-data=.../path/to/data.csv to be specified.")
	if save_model_path is None:
		raise WCMIError("error: train requires --save-model.../path/to/model.pt to be specified.")
	if num_epochs < 1:
		raise WCMIError("error: train requires --num-epochs to be at least 1.")

	# Read the CSV file.
	simulation_data = simulation.SimulationData(
		load_data_path=load_data_path,
		save_data_path=None,  # (This is for CSV prediction output, not MSE data.  Set to None.)
		verify_gan_n=True,
		optional_gan_n=True,
		gan_n=gan_n,
		simulation_info=simulation.simulation_info,
	)

	# Data verification.
	if len(simulation_data.data) <= 0:
		raise WCMIError("error: train requires at least one sample in the CSV file.")

	# Calculate sizes, numbers, and lengths.
	num_samples = len(simulation_data.data)
	num_testing_samples = int(round(data.test_proportion * num_samples))  # (redundant int())
	num_training_samples = num_samples - num_testing_samples

	if batch_size <= 0:
		batch_size = num_samples
	if batch_size > num_samples:
		batch_size = num_samples

	num_batches = (num_samples + batch_size - 1) // batch_size
	final_batch_size = num_samples % batch_size
	if final_batch_size == 0:
		final_batch_size = batch_size

	num_training_batches = (num_training_samples + batch_size - 1) // batch_size
	final_training_batch_size = num_training_samples % batch_size
	if final_training_batch_size == 0:
		final_training_batch_size = batch_size

	num_testing_batches = (num_testing_samples + batch_size - 1) // batch_size
	final_testing_batch_size = num_testing_samples % batch_size
	if final_testing_batch_size == 0:
		final_testing_batch_size = batch_size

	# Get the input and labels (target).
	num_sim_in_columns     = simulation_data.simulation_info.num_sim_inputs
	num_sim_in_out_columns = num_sim_in_columns + simulation_data.simulation_info.num_sim_outputs

	#npdata = simulation_data.data.values[:, :num_sim_in_out_columns]  # (No need for a numpy copy.)
	all_data = torch.Tensor(simulation_data.data.values[:, :num_sim_in_out_columns]).to(data.device)
	all_labels = all_data.view(all_data.shape)[:, :num_sim_in_columns]
	all_input  = all_data.view(all_data.shape)[:, num_sim_in_columns:num_sim_in_out_columns]

	# Get mean, stddev, min, and max of each input and label column for
	# standardization or normalization.
	all_nplabels  = all_labels.numpy()
	all_npinput   = all_input.numpy()
	label_means = torch.tensor(np.apply_along_axis(np.mean, axis=0, arr=all_nplabels))
	label_stds  = torch.tensor(np.apply_along_axis(np.std,  axis=0, arr=all_nplabels))
	label_mins  = torch.tensor(np.apply_along_axis(np.min,  axis=0, arr=all_nplabels))
	label_maxs  = torch.tensor(np.apply_along_axis(np.max,  axis=0, arr=all_nplabels))
	input_means = torch.tensor(np.apply_along_axis(np.mean, axis=0, arr=all_npinput))
	input_stds  = torch.tensor(np.apply_along_axis(np.std,  axis=0, arr=all_npinput))
	input_mins  = torch.tensor(np.apply_along_axis(np.min,  axis=0, arr=all_npinput))
	input_maxs  = torch.tensor(np.apply_along_axis(np.max,  axis=0, arr=all_npinput))

	# Load the model.
	#
	# Optionally, the model might be randomly initialized if it hasn't been
	# trained before.
	mdl        = gan.GAN          if use_gan else dense.Dense
	mdl_kwargs = {'gan_n': gan_n} if use_gan else {}
	model = mdl(
		load_model_path=load_model_path,
		save_model_path=save_model_path,
		auto_load_model=True,
		population_mean_in =input_means if not use_gan else [input_means, label_means],
		population_std_in  =input_stds  if not use_gan else [input_stds,  label_stds],
		population_min_in  =input_mins  if not use_gan else [input_mins,  label_mins],
		population_max_in  =input_maxs  if not use_gan else [input_maxs,  label_maxs],
		population_mean_out=label_means,
		population_std_out =label_stds,
		population_min_out =label_mins,
		population_max_out =label_maxs,
		standardize_bounds_multiple=use_gan,
		**mdl_kwargs,
	)
	# If CUDA is available, move the model to the GPU.
	model = model.to(data.device)

	# Split data into training data and test data.  The test data will be
	# invisible during the training (except to report accuracies).

	# Set a reproducible initial seed for a reproducible split, but then
	# reset the seed after the split.
	torch.random.manual_seed(data.testing_split_seed)

	# Shuffle the rows of data.
	#np.random.shuffle(npdata)
	# c.f. https://stackoverflow.com/a/53284632
	all_data = all_data[torch.randperm(all_data.size()[0])].to(data.device)

	# Restore randomness.
	#torch.random.manual_seed(torch.random.seed())
	# Fix an error, c.f.
	# https://discuss.pytorch.org/t/initial-seed-too-large/28832
	torch.random.manual_seed(torch.random.seed() & ((1<<63)-1))

	testing_data = all_data.view(all_data.shape)[:num_testing_samples]
	training_data = all_data.view(all_data.shape)[num_testing_samples:]

	testing_labels = testing_data.view(testing_data.shape)[:, :num_sim_in_columns]
	testing_input  = testing_data.view(testing_data.shape)[:, num_sim_in_columns:num_sim_in_out_columns]
	testing_gan_n  = testing_data.view(testing_data.shape)[:, num_sim_in_out_columns:]

	training_labels = training_data.view(training_data.shape)[:, :num_sim_in_columns]
	training_input  = training_data.view(training_data.shape)[:, num_sim_in_columns:num_sim_in_out_columns]
	training_gan_n  = training_data.view(training_data.shape)[:, num_sim_in_out_columns:]

	# Ensure the GAN generation columns are correctly numbered.
	if gan_force_fixed_gen_params:
		# Are fixed GAN generation parameters available?
		if training_data.shape[1] <= num_sim_in_out_columns:
			raise WCMIError("error: train: --gan-force-fixed-gen-params was specified, but no GAN generation parameters are available in the loaded CSV data.")
	if gan_n is not None and gan_n >= 1:
		gan_n_columns_available = training_data.shape[1] - num_sim_in_out_columns
		if gan_n_columns_available != gan_n and gan_n_columns_available != 0:
			raise WCMIError(
				"error: train: there are GAN gen columns present, but the number of GAN columns available in the input CSV data does not match the --gan-n variable: {0:d} != {1:d}".format(
					gan_n_columns_available, gan_n,
				)
			)

	# Let the user know on which device training is occurring.
	logger.info("device: {0:s}".format(str(data.device)))

	# Train the model.
	if not use_gan:
		# Get a tensor to store predictions for each epoch.  It will be
		# overwritten at each epoch.
		current_epoch_testing_errors = torch.zeros(testing_labels.shape, device=data.device, requires_grad=False)
		current_epoch_training_errors = torch.zeros(training_labels.shape, device=data.device, requires_grad=False)

		# After each epoch, set the corresponding element in this array to the
		# calculated MSE accuracy.
		epoch_training_mse = torch.zeros((num_epochs,num_sim_in_columns,), device=data.device)
		epoch_testing_mse = torch.zeros((num_epochs,num_sim_in_columns,), device=data.device)

		# Define the loss function and the optimizer.
		loss_function = nn.MSELoss()

		# Give the optimizer a reference to our model's parameters, which
		# include the model's weights and biases.  The optimizer will update
		# them.
		optimizer = torch.optim.SGD(
			model.parameters(),
			lr=learning_rate,
			momentum=data.momentum,
			weight_decay=data.weight_decay,
			dampening=data.dampening,
			nesterov=data.nesterov,
		)

		# Run all epochs.
		for epoch in range(num_epochs):
			# Should we print a status update?
			if status_every_epoch <= 0:
				status_enabled = False
			else:
				status_enabled = epoch % status_every_epoch == 0

			if status_enabled:
				#if epoch > 1:
				#	logger.info("")
				logger.info("")
				logger.info("Beginning epoch #{0:,d}/{1:,d}.".format(epoch + 1, num_epochs))

			# Shuffle the rows of data.
			training_data = training_data[torch.randperm(training_data.size()[0])].to(data.device)

			# Clear the error tensors for this epoch.
			current_epoch_testing_errors = torch.zeros(testing_labels.shape, out=current_epoch_testing_errors, device=data.device, requires_grad=False)
			current_epoch_training_errors = torch.zeros(training_labels.shape, out=current_epoch_training_errors, device=data.device, requires_grad=False)

			# Zero the gradient.
			optimizer.zero_grad()

			# Training phase: run all training batches in the epoch.
			for batch in range(num_training_batches):
				if not status_enabled or status_every_sample <= 0:
					substatus_enabled = False
				else:
					# Example:
					# 	0*2=0  % 4=0  < 2
					# 	1*2=2  % 4=2 !< 2
					# 	2*2=4  % 4=0  < 2
					# 	3*2=6  % 4=2 !< 2
					# 	4*2=8  % 4=0  < 2
					# 	5*2=10 % 4=2 !< 2
					substatus_enabled = batch * batch_size % status_every_sample < batch_size

				# Print a status for the next sample?
				if substatus_enabled:
					logger.info("  Beginning sample #{0:,d}/{1:,d} (epoch #{2:,d}/{3:,d}).".format(
						batch * batch_size + 1,
						num_samples,
						epoch + 1,
						num_epochs,
					))

				# Get this batch of samples.
				batch_slice = slice(batch * batch_size, (batch + 1) * batch_size)  # i.e. [batch * batch_size:(batch + 1) * batch_size]

				#batch_data   = training_data[batch_slice]
				batch_input  = training_input[batch_slice]
				batch_labels = training_labels[batch_slice]

				# Forward pass.
				batch_output = model(batch_input)
				loss = loss_function(batch_output, batch_labels)

				if substatus_enabled:
					logger.info("    MSE loss, mean of columns: {0:,f}".format(loss.item()))

				# Record the errors for this batch.
				current_epoch_training_errors[batch_slice] = batch_output.detach() - batch_labels.detach()

				# Backpropogate to calculate the gradient and then optimize to
				# update the weights (parameters).
				loss.backward()
				optimizer.step()

			# Calculate the MSE for each prediction column (7-element vector),
			# then assign it to epoch_mse_errors
			current_epoch_training_mse = (current_epoch_training_errors**2).mean(0)
			epoch_training_mse[epoch] = current_epoch_training_mse
			current_epoch_training_mse_norm = current_epoch_training_mse.norm()
			current_epoch_training_mse_mean = current_epoch_training_mse.mean()

			# Perform testing for this epoch.
			#
			# Disable gradient calculation during this phase with
			# torch.no_grad() since we're not doing backpropagation here.
			with torch.no_grad():
				# Now run the test batches.
				for batch in range(num_testing_batches):
					total_batch = batch + num_training_batches

					if not status_enabled or status_every_sample <= 0:
						substatus_enabled = False
					else:
						# Example:
						# 	0*2=0  % 4=0  < 2
						# 	1*2=2  % 4=2 !< 2
						# 	2*2=4  % 4=0  < 2
						# 	3*2=6  % 4=2 !< 2
						# 	4*2=8  % 4=0  < 2
						# 	5*2=10 % 4=2 !< 2
						substatus_enabled = total_batch * batch_size % status_every_sample < batch_size

					# Print a status for the next sample?
					if substatus_enabled:
						logger.info("  Beginning sample #{0:,d}/{1:,d} (testing phase) (epoch #{2:,d}/{3:,d}).".format(
							total_batch * batch_size + 1,
							num_samples,
							epoch + 1,
							num_epochs,
						))

					# Get this batch of samples.
					batch_slice = slice(batch * batch_size, (batch + 1) * batch_size)  # i.e. [batch * batch_size:(batch + 1) * batch_size]

					#batch_data   = testing_data[batch_slice]
					batch_input  = testing_input[batch_slice]
					batch_labels = testing_labels[batch_slice]

					# Forward pass.
					batch_output = model(batch_input)
					loss = loss_function(batch_output, batch_labels)

					if substatus_enabled:
						logger.info("    MSE loss, mean of columns: {0:,f}".format(loss.item()))

					# Record the errors for this batch.
					current_epoch_testing_errors[batch_slice] = batch_output.detach() - batch_labels.detach()

				# Calculate the MSE for each prediction column (7-element vector),
				# then assign it to epoch_mse_errors
				current_epoch_testing_mse = (current_epoch_testing_errors**2).mean(0)
				epoch_testing_mse[epoch] = current_epoch_testing_mse
				current_epoch_testing_mse_norm = current_epoch_testing_mse.norm()
				current_epoch_testing_mse_mean = current_epoch_testing_mse.mean()

			# Unless we're outputting a status message every epoch, let the
			# user know we've finished this epoch.
			#if status_enabled and status_every_epoch > 1:
			if status_enabled:
				logger.info(
					"Done training epoch #{0:,d}/{1:,d} (testing MSE norm (mean) vs. training MSE norm (mean): {2:,f} ({3:,f}) vs. {4:,f} ({5:,f}) (lower is more accurate)).".format(
						epoch + 1, num_epochs,
						current_epoch_testing_mse_norm,
						current_epoch_testing_mse_mean,
						current_epoch_training_mse_norm,
						current_epoch_training_mse_mean,
					)
				)

		# We are done training the Dense neural network.
		#
		# Get and print some stats.
		last_testing_mse = epoch_testing_mse[-1]
		last_training_mse = epoch_training_mse[-1]

		all_nplabels = all_labels.numpy()

		logger.info("")
		logger.info("Done training last epoch.  Preparing statistics...")

		def stat_format(fmt, tvec=None, lvec=None, float_str_min_len=13):
			"""
			Print a formatting line.  Specify tvec for a tensor vector (norm
			added) or lvec for a list of strings.
			"""
			if tvec is not None:
				return fmt.format(str(float_str_min_len)).format(
					"<{0:s}>".format(", ".join(["{{0:{0:s},f}}".format(str(float_str_min_len)).format(component) for component in tvec])),
					tvec.norm(),
					tvec.mean(),
				)
			elif lvec is not None:
				return fmt.format(str(float_str_min_len)).format(
					"<{0:s}>".format(", ".join(["{{0:>{0:s}s}}".format(str(float_str_min_len)).format(str(component)) for component in lvec])),
				)
			else:
				return fmt

		stat_fmts = (
			(False, "", None, None),
			(False, "Last testing MSE   (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", last_testing_mse, None),
			(True,  "Last testing RMSE  (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", last_testing_mse.sqrt(), None),
			(False, "Last training MSE  (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", last_training_mse, None),
			(False, "Last training RMSE (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", last_training_mse.sqrt(), None),
			(False, "", None, None),
			(False, "Label column names               : {{0:s}}", None, simulation_data.simulation_info.sim_input_names),
			(False, "", None, None),
			(False, "All labels mean    (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", all_labels.mean(0), None),
			(False, "All labels var     (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", all_labels.var(0), None),
			(True,  "All labels stddev  (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", all_labels.std(0), None),
			(False, "", None, None),
			(False, "All labels min     (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", torch.Tensor(np.quantile(all_nplabels, 0, 0)), None),
			(False, "...1st quartile    (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", torch.Tensor(np.quantile(all_nplabels, 0.25, 0)), None),
			(False, "All labels median  (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", torch.Tensor(np.quantile(all_nplabels, 0.5, 0)), None),
			(False, "...3rd quartile    (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", torch.Tensor(np.quantile(all_nplabels, 0.75, 0)), None),
			(False, "All labels max     (norm) (mean) : {{0:s}} ({{1:{0:s},f}}) ({{2:{0:s},f}})", torch.Tensor(np.quantile(all_nplabels, 1, 0)), None),
		)

		def stat_fmt_lines(float_str_min_len):
			"""Given a float_str_min_len value, return formatted stats lines."""
			return (*(
				(white, stat_format(*vals, float_str_min_len=float_str_min_len)) for white, *vals in stat_fmts
			),)
		def print_stat_fmt_lines(float_str_min_len, logger=logger):
			"""Given a float_str_min_len value, print formatted stats lines."""
			for white, line in stat_fmt_lines(float_str_min_len):
				if not white:
					logger.info(line)
				else:
					logger.info(line, color="white")

		# Start at 13 and decrease until the maximimum line length is <=
		# COLUMNS, then keep decreasing until the number of maximum lengthed
		# lines changes.
		float_str_min_len = 13
		if data.auto_size_formatting:
			cols = None
			columns, rows = shutil.get_terminal_size((1, 1))
			if columns > 1:
				cols = columns
			if cols is not None:
				# We know the columns, so we can be more liberal in the number we
				# start with.  Start at 30.
				float_str_min_len = 30

			last_max_line_len_count = None
			# Try decreasing to 0, inclusive.
			for try_float_str_min_len in range(float_str_min_len, -1, -1):
				lines = stat_fmt_lines(try_float_str_min_len)
				max_line_len = max([len(line) for white, line in lines])
				max_line_len_count = len([line for white, line in lines if len(line) >= max_line_len])
				if cols is None or max_line_len_count <= cols:
					if last_max_line_len_count is not None and max_line_len_count < last_max_line_len_count:
						break
				last_max_line_len_count = max_line_len_count
				float_str_min_len = try_float_str_min_len

		# Now print the stats.
		print_stat_fmt_lines(float_str_min_len, logger=logger)

		# Did the user specify to save MSE errors?
		if save_data_path is not None:
			mse_columns = ["is_training", *["mse_{0:s}".format(column) for column in simulation_data.simulation_info.sim_input_names]]
			# Prepend the "is_training" column as the first.
			epoch_training_np_mse = epoch_training_mse.numpy()
			epoch_testing_np_mse = epoch_testing_mse.numpy()
			testing_mse = np.concatenate(
				(
					np.zeros((num_epochs,1,)),
					epoch_testing_np_mse,
				),
				axis=1,
			)
			training_mse = np.concatenate(
				(
					np.ones((num_epochs,1,)),
					epoch_training_np_mse,
				),
				axis=1,
			)
			mse = np.concatenate(
				(
					testing_mse,
					training_mse,
				),
				axis=0,
			)
			mse_output = pd.DataFrame(
				data=mse,
				columns=mse_columns,
			)
			# c.f. https://stackoverflow.com/a/41591077
			mse_output["is_training"] = mse_output["is_training"].astype(int)
			mse_output.to_csv(save_data_path, index=False)

			logger.info("")
			logger.info("Wrote MSE errors (testing MSE for each epoch and then training MSE for each epoch) to `{0:s}'.".format(save_data_path))
	else:
		# Train the GAN model instead of the Dense model.

		# Define simple tensors for the actual GAN labels:
		generated_labels = torch.full((batch_size, 1), gan.GAN.GENERATED_LABEL_ITEM, device=data.device)
		real_labels      = torch.full((batch_size, 1), gan.GAN.REAL_LABEL_ITEM,      device=data.device)

		# Keep the generator and the discriminator loss in balance.  If the
		# loss of the other is more than threshold times this value, pause
		# training this one.
		pause_threshold = gan_training_pause_threshold

		# Within an epoch, per-sample losses.
		# Cleared each epoch.
		current_epoch_num_generator_training_samples = 0
		current_epoch_num_discriminator_training_samples = 0
		# discriminator_real_loss, discriminator_generated_loss, generator_loss
		current_epoch_training_losses = torch.zeros((num_training_samples,3,), device=data.device, requires_grad=False)
		current_epoch_testing_losses = torch.zeros((num_testing_samples,3,), device=data.device, requires_grad=False)

		# Per-epoch losses.
		#
		# After each epoch, set the corresponding element in this array to the
		# calculated BCE loss for each column, obtained by finding the mean BCE
		# loss for each sample within a given column.
		epoch_losses_columns = [
			# What was the mean discriminator loss for this epoch during
			# training for real data?
			"training_mean_discriminator_real_bce_loss",

			# What was the mean discriminator loss for this epoch during
			# training for generated data?
			"training_mean_discriminator_generated_bce_loss",

			# What was the mean generator loss in the adversarial network for
			# this epoch during training?
			"training_mean_generator_bce_loss",

			# During the testing phase in this epoch, what was the mean BCE
			# loss for the discriminator for real data?
			"testing_mean_discriminator_real_bce_loss",

			# During the testing phase in this epoch, what was the mean BCE
			# loss for the discriminator for generated data?
			"testing_mean_discriminator_generated_bce_loss",

			# During the testing phase in this epoch, what was the mean BCE
			# loss for the generator?  How good was it at fooling the
			# discriminator, when using test input?
			"testing_mean_generator_bce_loss",

			# How many training samples were there in the dataset?
			"num_training_samples",

			# For how many samples was the discriminator training paused during
			# the testing this epoch?
			"num_discriminator_training_paused",

			# For how many samples was the generator training paused during
			# the training of this epoch?
			"num_generator_training_paused",
		]
		epoch_losses = torch.zeros((num_epochs, len(epoch_losses_columns),), device=data.device, requires_grad=False)

		# Define the loss function and the optimizers.
		loss_function = nn.BCELoss(reduction="none")

		# Give the optimizer a reference to our model's parameters, which
		# include the model's weights and biases.  The optimizer will update
		# them.
		#
		# c.f. https://pytorch.org/tutorials/beginner/dcgan_faces_tutorial.html
		generator_optimizer = torch.optim.SGD(
			model.generator.parameters(),
			lr=learning_rate,
			momentum=data.momentum,
			weight_decay=data.weight_decay,
			dampening=data.dampening,
			nesterov=data.nesterov,
		)

		discriminator_optimizer = torch.optim.SGD(
			model.discriminator.parameters(),
			lr=learning_rate,
			momentum=data.momentum,
			weight_decay=data.weight_decay,
			dampening=data.dampening,
			nesterov=data.nesterov,
		)

		# Run all epochs.
		for epoch in range(num_epochs):
			# Should we print a status update?
			if status_every_epoch <= 0:
				status_enabled = False
			else:
				status_enabled = epoch % status_every_epoch == 0

			if status_enabled:
				#if epoch > 1:
				#	logger.info("")
				logger.info("")
				logger.info("Beginning epoch #{0:,d}/{1:,d}.".format(epoch + 1, num_epochs))

			# Shuffle the rows of data.
			training_data = training_data[torch.randperm(training_data.size()[0])].to(data.device)

			# Clear the current epoch data for this epoch.
			current_epoch_num_generator_training_samples = 0
			current_epoch_num_discriminator_training_samples = 0
			# discriminator_real_loss, discriminator_generated_loss, generator_loss
			current_epoch_training_losses = torch.zeros(
				current_epoch_training_losses.shape,
				out=current_epoch_training_losses,
				device=data.device, requires_grad=False,
			)
			current_epoch_testing_losses = torch.zeros(
				current_epoch_testing_losses.shape,
				out=current_epoch_testing_losses,
				device=data.device, requires_grad=False,
			)

			# Zero the gradient.
			generator_optimizer.zero_grad()
			discriminator_optimizer.zero_grad()

			# Training phase: run all training batches in the epoch.
			for batch in range(num_training_batches):
				if not status_enabled or status_every_sample <= 0:
					substatus_enabled = False
				else:
					# Example:
					# 	0*2=0  % 4=0  < 2
					# 	1*2=2  % 4=2 !< 2
					# 	2*2=4  % 4=0  < 2
					# 	3*2=6  % 4=2 !< 2
					# 	4*2=8  % 4=0  < 2
					# 	5*2=10 % 4=2 !< 2
					substatus_enabled = batch * batch_size % status_every_sample < batch_size

				# Print a status for the next sample?
				if substatus_enabled:
					logger.info("  Beginning sample #{0:,d}/{1:,d} (epoch #{2:,d}/{3:,d}).".format(
						batch * batch_size + 1,
						num_samples,
						epoch + 1,
						num_epochs,
					))

				# Get this batch of samples.
				batch_slice = slice(batch * batch_size, (batch + 1) * batch_size)  # i.e. [batch * batch_size:(batch + 1) * batch_size]

				#batch_data             = training_data[batch_slice]
				batch_input            = training_input[batch_slice]
				batch_labels           = training_labels[batch_slice]

				batch_generated_labels = generated_labels[:len(batch_input)]
				batch_real_labels      = real_labels[:len(batch_input)]

				# Does the user want fixed GAN generation parameters?
				if gan_force_fixed_gen_params:
					gan_gen_params = training_gan_n.view(training_gan_n.shape)[batch_slice]
				else:
					# Don't use fixed GAN generation parameters.
					# Generate random generation parameters.
					gan_gen_params = torch.rand((len(batch_input), gan_n), device=data.device)

				# Train:
				# 	Discriminator:
				# 		One batch of real data.
				# 		One batch of generated data.
				# 	Generator:
				# 		Same generated data in the previous step.

				# Forward passes.

				# Discriminator: forward pass one batch of real data.
				discriminator_real_output = model(batch_input, batch_labels, subnetwork_selection=gan.GAN.GANSubnetworkSelection.DISCRIMINATOR_ONLY)
				discriminator_real_loss_unreduced = loss_function(discriminator_real_output, batch_real_labels)
				discriminator_real_loss = discriminator_real_loss_unreduced.mean()

				# Generate a batch of generated_data
				generator_output = model(batch_input, gan_gen_params, subnetwork_selection=gan.GAN.GANSubnetworkSelection.GENERATOR_ONLY)

				# Discriminator: forward pass one batch of generated data.
				discriminator_generated_output = model(batch_input, batch_labels, subnetwork_selection=gan.GAN.GANSubnetworkSelection.DISCRIMINATOR_ONLY)
				discriminator_generated_loss_unreduced = loss_function(discriminator_generated_output, batch_generated_labels)
				discriminator_generated_loss = discriminator_generated_loss_unreduced.mean()

				# Get the mean discriminator loss.
				#discriminator_loss_unreduced = discriminator_real_loss + (discriminator_generated_loss - discriminator_real_loss )/2
				discriminator_loss = np.mean((discriminator_real_loss.item(), discriminator_generated_loss.item()))

				# Generator: get loss for the same forward pass.
				generator_loss_unreduced = loss_function(discriminator_generated_output, batch_real_labels)
				generator_loss = generator_loss_unreduced.mean()

				# Determine which subnetwork trainings to pause.
				if not gan_enable_pause:
					pause_discriminator = False
					pause_generator     = False
				#elif pause_threshold is None or pause_min_samples_per_epoch is None or pause_min_epochs is None or pause_max_epochs is None:
				#	pause_discriminator = False
				#	pause_generator     = False
				elif epoch < pause_min_epochs:
					pause_discriminator = False
					pause_generator     = False
				elif pause_max_epochs > 0 and epoch > pause_max_epochs:
					pause_discriminator = False
					pause_generator     = False
				elif current_epoch_num_discriminator_training_samples < pause_min_samples_per_epoch:
					pause_discriminator = False
					pause_generator     = False
				else:
					pause_discriminator = discriminator_loss <= generator_loss - pause_threshold
					pause_generator     = generator_loss <= discriminator_loss - pause_threshold

				# Train the discriminator if it isn't outperforming the
				# generator by too much.
				if not pause_discriminator:
					current_epoch_num_discriminator_training_samples += len(batch_input)

					# Backpropogate to calculate the gradient and then optimize to
					# update the weights (parameters).
					discriminator_real_loss.backward(retain_graph=True)
					discriminator_optimizer.step()

					discriminator_generated_loss.backward(retain_graph=not pause_generator)
					discriminator_optimizer.step()

				# Train the generator if it isn't outperforming the
				# discriminator by too much.
				if not pause_generator:
					current_epoch_num_generator_training_samples += len(batch_input)

					generator_loss.backward()
					generator_optimizer.step()

				# Record the losses for this batch.
				current_epoch_training_losses[batch_slice] = torch.stack(
					(discriminator_real_loss_unreduced.detach()[:, 0], discriminator_generated_loss_unreduced.detach()[:, 0], generator_loss_unreduced.detach()[:, 0]),
					axis=1,
				)

			# Perform the testing phase for this epoch.
			#
			# Disable gradient calculation during this phase with
			# torch.no_grad() since we're not doing backpropagation here.
			with torch.no_grad():
				for batch in range(num_testing_batches):
					total_batch = batch + num_training_batches

					if not status_enabled or status_every_sample <= 0:
						substatus_enabled = False
					else:
						substatus_enabled = total_batch * batch_size % status_every_sample < batch_size

					# Print a status for the next sample?
					if substatus_enabled:
						logger.info("  Beginning sample #{0:,d}/{1:,d} (testing phase) (epoch #{2:,d}/{3:,d}).".format(
							total_batch * batch_size + 1,
							num_samples,
							epoch + 1,
							num_epochs,
						))

					# Get this batch of samples.
					batch_slice = slice(batch * batch_size, (batch + 1) * batch_size)  # i.e. [batch * batch_size:(batch + 1) * batch_size]

					#batch_data             = testing_data[batch_slice]
					batch_input            = testing_input[batch_slice]
					batch_labels           = testing_labels[batch_slice]

					batch_generated_labels = generated_labels[:len(batch_input)]
					batch_real_labels      = real_labels[:len(batch_input)]

					# Does the user want fixed GAN generation parameters?
					if gan_force_fixed_gen_params:
						gan_gen_params = testing_gan_n.view(testing_gan_n.shape)[batch_slice]
					else:
						# Don't use fixed GAN generation parameters.
						# Generate random generation parameters.
						gan_gen_params = torch.rand((len(batch_input), gan_n), device=data.device)

					# Testing:
					# 	Discriminator:
					# 		One batch of real data.
					# 		One batch of generated data.
					# 	Generator:
					# 		Same generated data in the previous step.

					# Forward passes.

					# Discriminator: forward pass one batch of real data.
					discriminator_real_output = model(batch_input, batch_labels, subnetwork_selection=gan.GAN.GANSubnetworkSelection.DISCRIMINATOR_ONLY)
					discriminator_real_loss_unreduced = loss_function(discriminator_real_output, batch_real_labels)
					discriminator_real_loss = discriminator_real_loss_unreduced.mean()

					# Generate a batch of generated_data
					generator_output = model(batch_input, gan_gen_params, subnetwork_selection=gan.GAN.GANSubnetworkSelection.GENERATOR_ONLY)

					# Discriminator: forward pass one batch of generated data.
					discriminator_generated_output = model(batch_input, batch_labels, subnetwork_selection=gan.GAN.GANSubnetworkSelection.DISCRIMINATOR_ONLY)
					discriminator_generated_loss_unreduced = loss_function(discriminator_generated_output, batch_generated_labels)
					discriminator_generated_loss = discriminator_generated_loss_unreduced.mean()

					# Generator: get loss for the same forward pass.
					generator_loss_unreduced = loss_function(discriminator_generated_output, batch_real_labels)
					generator_loss = generator_loss_unreduced.mean()

					# Record the losses for this batch.
					current_epoch_testing_losses[batch_slice] = torch.stack(
						(discriminator_real_loss_unreduced.detach()[:, 0], discriminator_generated_loss_unreduced.detach()[:, 0], generator_loss_unreduced.detach()[:, 0]),
						axis=1,
					)

			# We're almost done with this epoch.  Just store our results for
			# this epoch.

			# "training_mean_discriminator_real_bce_loss"
			epoch_losses[epoch][0] = current_epoch_training_losses[:, 0].mean().item()

			# "training_mean_discriminator_generated_bce_loss"
			epoch_losses[epoch][1] = current_epoch_training_losses[:, 1].mean().item()

			# "training_mean_generator_bce_loss"
			epoch_losses[epoch][2] = current_epoch_training_losses[:, 2].mean().item()

			# "testing_mean_discriminator_real_bce_loss"
			epoch_losses[epoch][3] = current_epoch_testing_losses[:, 0].mean().item()

			# "testing_mean_discriminator_generated_bce_loss"
			epoch_losses[epoch][4] = current_epoch_testing_losses[:, 1].mean().item()

			# "testing_mean_generator_bce_loss"
			epoch_losses[epoch][5] = current_epoch_testing_losses[:, 2].mean().item()

			# "num_training_samples"
			epoch_losses[epoch][6] = num_training_samples

			# "num_discriminator_training_paused"
			epoch_losses[epoch][7] = num_training_samples - current_epoch_num_discriminator_training_samples

			# "num_generator_training_paused"
			epoch_losses[epoch][8] = num_training_samples - current_epoch_num_generator_training_samples

			# Let the user know we've finished this epoch.
			if status_enabled:
				logger.info(
					"Done training epoch #{0:,d}/{1:,d} (mean testing gen, disc_real, disc_gen loss: {2:f}, {3:f}, {4:f}) (mean training gen, disc_real, disc_gen loss: {5:f}, {6:f}, {7:f}) (paused gen, disc: {8:d}, {9:d}).".format(
						epoch + 1,
						num_epochs,

						epoch_losses[epoch][0],
						epoch_losses[epoch][1],
						epoch_losses[epoch][2],
						epoch_losses[epoch][3],
						epoch_losses[epoch][4],
						epoch_losses[epoch][5],

						int(round(epoch_losses[epoch][7].item())),
						int(round(epoch_losses[epoch][8].item())),
					)
				)

		# We are done training the GAN neural network.
		#
		# Optionally get and print some stats here.

		# Did the user specify to save BCE errors?
		if save_data_path is not None:
			bce_output = pd.DataFrame(
				data=epoch_losses,
				columns=epoch_losses_columns,
			)
			# c.f. https://stackoverflow.com/a/41591077
			for int_column in [
				"num_training_samples",
				"num_discriminator_training_paused",
				"num_generator_training_paused",
			]:
				bce_output[int_column] = bce_output[int_column].astype(int)
			bce_output.to_csv(save_data_path, index=False)

			logger.info("")
			logger.info("Wrote training epoch data to `{0:s}'.".format(save_data_path))

	# Save the trained model.
	model.save(logger=logger)
	logger.info("")
	logger.info("Saved trained model to `{0:s}'.".format(model.save_model_path))

	# We're done.  Catch you later.
	logger.info("")
	logger.info("Done training all epochs.")
	logger.info("Have a good day.")

def run(
	use_gan=True, load_model_path=None, load_data_path=None,
	save_data_path=None, gan_n=gan.default_gan_n,
	output_keep_out_of_bounds_samples=False, logger=logger,
):
	"""
	Load the CSV data, pass it through the neural network, and write a new CSV
	file that includes what the neural network predicted.

	Run the application with --help for more information on the structure of
	the loaded and written CSV files.
	"""
	# Default arguments.
	if gan_n is None:
		gan_n = gan.default_gan_n

	# Argument verification.
	if load_data_path is None:
		raise WCMIError("error: run requires --load-data=.../path/to/data.csv to be specified.")
	if save_data_path is None:
		raise WCMIError("error: run requires --save-data=.../path/to/data.csv to be specified.")
	if load_model_path is None:
		raise WCMIError("error: run requires --load-model=.../path/to/model.pt to be specified.")

	# Read the CSV file.
	simulation_data = simulation.SimulationData(
		load_data_path=load_data_path,
		save_data_path=save_data_path,
		verify_gan_n=True,
		optional_gan_n=True,
		gan_n=gan_n,
		simulation_info=simulation.simulation_info,
	)

	# Ensure there is at least one sample.
	if len(simulation_data.data) <= 0:
		raise WCMIError("error: run requires the CSV data loaded to contain at least one sample.")

	# Load the model.
	mdl        = gan.GAN          if use_gan else dense.Dense
	mdl_kwargs = {'gan_n': gan_n} if use_gan else {}
	model = mdl(
		load_model_path=load_model_path,
		save_model_path=None,
		auto_load_model=True,
		**mdl_kwargs,
	)

	# Feed the data to the model and collect the output.
	num_sim_in_columns     = simulation_data.simulation_info.num_sim_inputs
	num_sim_in_out_columns = num_sim_in_columns + simulation_data.simulation_info.num_sim_outputs

	#npdata = simulation_data.data.values[:, :num_sim_in_out_columns]  # (No need for a numpy copy.)
	all_data = torch.tensor(simulation_data.data.values[:, :num_sim_in_out_columns], dtype=torch.float32, device=data.device, requires_grad=False)
	all_labels = all_data.view(all_data.shape)[:, :num_sim_in_columns]
	all_input  = all_data.view(all_data.shape)[:, num_sim_in_columns:num_sim_in_out_columns]
	all_gan_n  = all_data.view(all_data.shape)[:, num_sim_in_out_columns:]

	if all_gan_n.shape[1] != gan_n and all_gan_n.shape[1] != 0:
		raise WCMIError(
			"error: run: there are GAN gen columns present, but the number of GAN columns available in the input CSV data does not match the --gan-n variable: {0:d} != {1:d}".format(
				all_gan_n.shape[1], gan_n,
			)
		)
	gan_fixed_gen = all_gan_n.shape[1] != 0

	## Pass the numpy array through the model.
	gan_gen_params = None
	if not use_gan:
		with torch.no_grad():
			all_output = model(all_input)
	else:
		if gan_fixed_gen:
			gan_gen_params = data_gan_n
		else:
			# Don't use fixed GAN generation parameters.
			# Generate random generation parameters.
			gan_gen_params = torch.rand((len(all_input), gan_n), device=data.device)

		with torch.no_grad():
			all_output = model(all_input, gan_gen_params)
	npoutput=all_output.numpy()

	## Reconstruct the Pandas frame with appropriate columns.
	input_columns = simulation_data.data.columns.values.tolist()

	predicted_columns = ["pred_{0:s}".format(name) for name in input_columns[:num_sim_in_columns]]

	output_columns = input_columns[:]
	output_columns[num_sim_in_out_columns:num_sim_in_out_columns] = predicted_columns[:]

	## Construct a new npoutput with the 7 new prediction columns added.
	npdata_extra = simulation_data.data.values
	expanded_npoutput = np.concatenate(
		(
			npdata_extra[:, :num_sim_in_out_columns],
			npoutput,
			npdata_extra[:, num_sim_in_out_columns:] if gan_fixed_gen or gan_gen_params is None else gan_gen_params.numpy(),
		),
		axis=1,
	)

	if use_gan:
		# If the input columns lacked GAN columns, then add them now, since the
		# GAN columns are present.
		if not gan_fixed_gen:
			# No GAN columns.  Add them.
			output_columns += ["GAN_{0:d}".format(gan_column_num) for gan_column_num in range(gan_n)]

	output = pd.DataFrame(
		data=expanded_npoutput,
		columns=output_columns,
	)

	# Check boundaries.
	input_npmins = np.array(simulation_data.simulation_info.sim_input_mins)
	input_npmaxs = np.array(simulation_data.simulation_info.sim_input_maxs)
	if not output_keep_out_of_bounds_samples:
		# Get a mask of np.array([True, True, True, False, True, ...]) as to which rows are
		# valid.
		input_npmins_repeated = np.repeat(np.array([input_npmins]), npoutput.shape[0], axis=0)
		input_npmaxs_repeated = np.repeat(np.array([input_npmaxs]), npoutput.shape[0], axis=0)
		min_valid_npoutput = npoutput >= input_npmins_repeated
		max_valid_npoutput = npoutput <= input_npmaxs_repeated
		valid_npoutput = np.logical_and(min_valid_npoutput, max_valid_npoutput)
		#valid_npoutput_samples = np.apply_along_axis(all, axis=1, arr=valid_npoutput)[:,np.newaxis]  # Reduce rows by "and".
		valid_npoutput_mask = np.apply_along_axis(all, axis=1, arr=valid_npoutput)  # Reduce rows by "and" and get a flat, 1-D vector.

		# Only keep valid rows in output.
		old_num_samples = len(output)
		output = output.iloc[valid_npoutput_mask]
		new_num_samples = len(output)
		num_lost_samples = old_num_samples - new_num_samples

		if num_lost_samples <= 0:
			logger.info("All model predictions are within the minimum and maximum boundaries.")
			logger.info("")
		else:
			logger.warning("WARNING: #{0:,d}/#{0:,d} sample rows have been discarded from the CSV output due to out-of-bounds predictions.".format(num_lost_samples, old_num_samples))
			logger.warning("")

	# Make sure the output isn't all the same.
	if len(npoutput) >= 2:
		npoutput_means = np.apply_along_axis(np.std, axis=0, arr=npoutput)
		npoutput_stds = np.apply_along_axis(np.std, axis=0, arr=npoutput)
		# Warn if the std is <= this * (max_bound - min_bound).
		std_warn_threshold = 0.1
		num_warnings = 0
		# (Per-column.)
		unique_warn_threshold = 25

		min_val, max_val = np.min(npoutput), np.max(npoutput)
		max_val_str_len = max(len(str(min_val)), len(str(max_val)))

		all_unique = np.unique(npoutput)

		min_unique_val, max_unique_val = np.min(all_unique), np.max(all_unique)
		max_unique_val_str_len = max(len(str(min_unique_val)), len(str(max_unique_val)))

		for idx, name in enumerate(simulation_data.data.columns.values[:simulation_data.simulation_info.num_sim_inputs]):
			if num_warnings >= 1:
				logger.warning("")

			std = npoutput_stds[idx]
			this_threshold = std_warn_threshold * (input_npmaxs[idx] - input_npmins[idx])
			if std <= 0.0:
				logger.warning("WARNING: all predictions for simulation input parameter #{0:d} (`{1:s}`) are the same!  Prediction: {2:,f}.".format(idx + 1, name, npoutput[0][idx]))
				num_warnings += 1
			elif std <= this_threshold:
				logger.warning("WARNING: there is little variance in the predictions for simulation input parameter #{0:d} (`{1:s}`): std <= this_threshold: {2:,f} <= {3:,f}.".format(idx + 1, name, std, this_threshold))
				num_warnings += 1

			# Count unique values and warn if there are few.
			col = npoutput[:,idx]
			#unique = set(npoutput[:,idx].tolist())
			unique = np.unique(npoutput[:,idx])

			min_unique_val, max_unique_val = np.min(unique), np.max(unique)
			max_unique_val_str_len = max(len(str(min_unique_val)), len(str(max_unique_val)))

			if len(unique) <= unique_warn_threshold:
				logger.warning("WARNING: there are few unique values (#{0:,d}) for predictions for simulation input parameter #{1:d} (`{2:s}`):".format(
					len(unique), idx + 1, name
				))
				num_warnings += 1

				max_unique = max(unique)
				len_str_max_unique = len(str(max_unique))

				min_val_str_len = max_unique_val_str_len if True else len_str_max_unique

				float_groups = []
				visited = set()
				for val in sorted(list(unique)):
					if val not in visited:
						visited.add(val)
						lines = []

						count_eq    = len([x for x in col if x == val])
						close_values = [x for x in col if math.isclose(x, val)]
						count_close = len(close_values)

						float_groups.append((count_close, val, lines))

						if count_close > 1:
							if count_eq > count_close:
								lines.append("  {{0:<{0:d}s}} x{{1:,d}} close values:".format(max_val_str_len).format(str(val), count_close))
								for close_val in close_values:
									visited.add(close_val)

									count_eq = len([x for x in col if x == close_val])
									if count_eq > 1:
										lines.append("  {{0:<{0:d}s}} x{{1:,d}}".format(max_val_str_len).format(str(close_val), count_eq))
									else:
										lines.append("  {{0:<{0:d}s}}".format(max_val_str_len).format(str(close_val)))
							else:
								lines.append("  {{0:<{0:d}s}} x{{1:,d}}".format(max_val_str_len).format(str(val), count_eq))
						else:
							lines.append("  {{0:<{0:d}s}}".format(max_val_str_len).format(str(val)))
				for count_close, val, lines in sorted(float_groups, reverse=True):
					for line in lines:
						logger.warning(line)
		if num_warnings >= 1:
			logger.warning("")

	# Print MSE for each column.
	nplabels = all_labels.numpy()

	nperrors = (npoutput - nplabels)**2

	mse_npmeans = np.apply_along_axis(np.mean, axis=0, arr=nperrors)
	rmse_npmeans = np.sqrt(mse_npmeans)
	mse_mean = mse_npmeans.mean()
	rmse_mean = rmse_npmeans.mean()

	labels_npvar = np.apply_along_axis(np.var, axis=0, arr=nplabels)
	labels_npstd = np.apply_along_axis(np.std, axis=0, arr=nplabels)

	logger.info("")
	logger.info("Columns: <{0:s}>".format(", ".join(simulation_data.simulation_info.sim_input_names)))
	logger.info("")
	logger.info("Prediction MSEs for each column: <{0:s}>".format(", ".join("{0:f}".format(x) for x in mse_npmeans)))
	logger.info("Label variance for each column: <{0:s}>".format(", ".join("{0:f}".format(x) for x in labels_npvar)))
	logger.info("")
	logger.info("Prediction RMSEs for each column: <{0:s}>".format(", ".join("{0:f}".format(x) for x in rmse_npmeans)))
	logger.info("Label stddev for each column: <{0:s}>".format(", ".join("{0:f}".format(x) for x in labels_npstd)))
	logger.info("")
	logger.info("Mean of column MSEs: {0:f}".format(mse_mean))
	logger.info("Mean of label variances: {0:f}".format(labels_npvar.mean()))
	logger.info("")
	logger.info("Mean of column RMSEs: {0:f}".format(rmse_mean))
	logger.info("Mean of label stddevs: {0:f}".format(labels_npstd.mean()))

	# Write the output.
	simulation_data.save(output)
	logger.info("Wrote CSV output with predictions to `{0:s}'.".format(save_data_path))

def stats(save_data_path, logger=logger):
	"""
	(To be documented...)
	"""
	logger.error("(To be implemented...)")
	raise NotImplementedError("error: stats: the stats action is not yet implemented.")
	pass

def generate(save_data_path, logger=logger):
	"""
	TODO: document and clean up.
	"""
	simulation_info=simulation.simulation_info
	import random
	with open(save_data_path, "w") as f:
		f.write(
			",".join(simulation_info.sim_input_names + simulation_info.sim_output_names) + "\n",
		)
		for i in range(10000):
			f.write(
				",".join("{0:f}".format(zero) for zero in simulation_info.num_sim_inputs * (0.0,)) + "," + ",".join("{0:f}".format(random.randrange(min, max)) for min, max in simulation_info.get_sim_output_ranges()) + "\n",
			)
