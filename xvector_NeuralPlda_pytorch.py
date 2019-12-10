#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Oct  3 21:15:54 2019

@author: shreyasr, prashantk
"""

from __future__ import print_function
import argparse
import re

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
import random
import pickle
import re
import subprocess
from pdb import set_trace as bp

from utils.sv_trials_loaders import dataloader_from_trial, get_spk2xvector, generate_scores_from_net, \
    generate_scores_in_batches, xv_pairs_from_trial, concatenate_datasets, get_train_dataset, dataset_from_trial, \
    dataset_from_sre08_10_trial, load_xvec_from_batch, generate_scores_from_plda
from utils.calibration import get_cmn2_thresholds
from utils.Kaldi2NumpyUtils.kaldiPlda2numpydict import kaldiPlda2numpydict

from utils.sre08_10_prep import get_sre08_trials_etc, get_sre10_trials_etc
from datetime import datetime
import logging

timestamp = int(datetime.timestamp(datetime.now()))
print(timestamp)
logging.basicConfig(filename='logs/kaldiplda_{}.log'.format(timestamp),
                    filemode='a',
                    format='%(levelname)s: %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.DEBUG)


class NeuralPlda(nn.Module):
    def __init__(self, xdim=512, LDA_dim=170, PLDA_dim=170, device=torch.device("cuda")):
        super(NeuralPlda, self).__init__()
        self.centering_and_LDA = nn.Linear(xdim, LDA_dim)  # Centering, wccn
        self.centering_and_wccn_plda = nn.Linear(LDA_dim, PLDA_dim)
        self.P_sqrt = nn.Parameter(torch.rand(PLDA_dim, requires_grad=True))
        self.Q = nn.Parameter(torch.rand(PLDA_dim, requires_grad=True))
        self.threshold1 = nn.Parameter(0*torch.rand(1, requires_grad=True)) # nn.Parameter(torch.tensor(4.5951)).to(device)
        self.threshold2 = nn.Parameter(0.7+0*torch.rand(1, requires_grad=True)) # nn.Parameter(torch.tensor(5.2933)).to(device)
        self.threshold1.requires_grad = False
        self.threshold2.requires_grad = False
        self.threshold_Xent = nn.Parameter(0*torch.rand(1, requires_grad=False))  # torch.nn.Parameter(0*torch.rand(1,requires_grad=True))
        self.alpha = torch.tensor(5.0).to(device)


    def forward(self, x1, x2):
        x1 = self.centering_and_LDA(x1)
        x2 = self.centering_and_LDA(x2)
        x1 = F.normalize(x1)
        x2 = F.normalize(x2)

        x1 = self.centering_and_wccn_plda(x1)
        x2 = self.centering_and_wccn_plda(x2)
        P = self.P_sqrt * self.P_sqrt
        Q = self.Q
        S = (x1 * Q * x1).sum(dim=1) + (x2 * Q * x2).sum(dim=1) + 2 * (x1 * P * x2).sum(dim=1)

        return S

    def LoadPldaParamsFromKaldi(self, mean_vec_file, transform_mat_file, PldaFile):
        plda = kaldiPlda2numpydict(PldaFile)
        transform_mat = np.asarray([w.split() for w in np.asarray(
            subprocess.check_output(["copy-matrix", "--binary=false", transform_mat_file, "-"]).decode('utf-8').strip()[
            2:-2].split('\n'))]).astype(float)
        mean_vec = np.asarray(
            subprocess.check_output(["copy-vector", "--binary=false", mean_vec_file, "-"]).decode('utf-8').strip()[
            1:-2].split()).astype(float)
        mdsd = self.state_dict()
        mdsd['centering_and_LDA.weight'].data.copy_(torch.from_numpy(transform_mat[:, :-1]).float())
        mdsd['centering_and_LDA.bias'].data.copy_(
            torch.from_numpy(transform_mat[:, -1] - transform_mat[:, :-1].dot(mean_vec)).float())
        mdsd['centering_and_wccn_plda.weight'].data.copy_(torch.from_numpy(plda['diagonalizing_transform']).float())
        mdsd['centering_and_wccn_plda.bias'].data.copy_(
            torch.from_numpy(-plda['diagonalizing_transform'].dot(plda['plda_mean'])).float())
        mdsd['P_sqrt'].data.copy_(torch.from_numpy(np.sqrt(plda['diagP'])).float())
        mdsd['Q'].data.copy_(torch.from_numpy(plda['diagQ']).float())

    def SaveModel(self, filename):
        with open(filename, 'wb') as f:
            pickle.dump(self, f)


def train(args, model, device, train_loader, valid_loader, mega_xvec_dict, num_to_id_dict, optimizer, epoch, ):
    model.eval()
    minC_threshold1, minC_threshold2, min_cent_threshold = compute_minc_threshold(args, model, device, mega_xvec_dict,
                                                                                  num_to_id_dict, valid_loader)
    model.train()
    softcdets = []
#    crossentropies = []
    fa1 = 0
    miss1 = 0
    fa2 = 0
    miss2 = 0
    tgt_count = 0
    non_tgt_count = 0
    nbatchCdet = 1
    meansoftcdet = 1
#    bp()
    for batch_idx, (data1, data2, target) in enumerate(train_loader):
        data1, data2, target = data1.to(device), data2.to(device), target.to(device)
        data1_xvec, data2_xvec = load_xvec_from_batch(mega_xvec_dict, num_to_id_dict, data1, data2, device)
        optimizer.zero_grad()
        output = model(data1_xvec, data2_xvec)
        sigmoid = nn.Sigmoid()
        loss1 = (sigmoid(model.alpha * (model.threshold1 - output)) * target).sum() / (target.sum()) + 99 * (
                sigmoid(model.alpha * (output - model.threshold1)) * (1 - target)).sum() / ((1 - target).sum())
        loss2 = (sigmoid(model.alpha * (model.threshold2 - output)) * target).sum() / (target.sum()) + 199 * (
                sigmoid(model.alpha * (output - model.threshold2)) * (1 - target)).sum() / ((1 - target).sum())
#        loss_bce = F.binary_cross_entropy(sigmoid(output - model.threshold_Xent), target)
#        bce1 = -(1/len(target)) * ((target*torch.log(sigmoid(output - model.threshold1))).sum() + 99*((1 - target)*torch.log(1-sigmoid(output - model.threshold1))).sum())
#        bce2 = -(1/len(target)) * ((target*torch.log(sigmoid(output - model.threshold2))).sum() + 199*((1 - target)*torch.log(1-sigmoid(output - model.threshold2))).sum())
#        bce3 = -(1/len(target)) * ((target*torch.log(sigmoid(output - model.threshold1))).sum() + ((1 - target)*torch.log(1-sigmoid(output - model.threshold2))).sum())
        loss = 0.5 * (loss1 + loss2)
#        0.5 * (bce1 + bce2)(loss1 + loss2) # bce3
        #  + loss_bce # Change to  0.1*loss_bce #or # 0.5*(loss1+loss2) #When required
        tgt_count += target.sum().item()
        non_tgt_count += (1 - target).sum().item()
        fa1 += ((output > model.threshold1).float() * (1 - target)).sum().item()
        miss1 += ((output < model.threshold1).float() * target).sum().item()
        fa2 += ((output > model.threshold2).float() * (1 - target)).sum().item()
        miss2 += ((output < model.threshold2).float() * target).sum().item()
        softcdet = (loss1.item() + loss2.item()) / 2
        softcdets.append(softcdet)
        if softcdet!=softcdet:
            bp()
#        crossentropies.append(loss_bce.item())
        loss.backward()
        optimizer.step()
        with open('logs/Thresholds_{}'.format(timestamp), 'a+') as f:
            f.write('{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n'.format(epoch, batch_idx, float(model.threshold1.data[0]), float(model.threshold2.data[0]), float(model.threshold_Xent.data[0]), minC_threshold1, minC_threshold2, meansoftcdet, nbatchCdet))
        if batch_idx % args.log_interval == 0:
#            bp()
            meansoftcdet = np.mean(softcdets)
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\t SoftCdet: {:.6f}'.format(
                epoch, batch_idx * len(data1), len(train_loader.dataset),
                       100. * batch_idx / len(train_loader), np.mean(softcdets)))
            logging.info('Train Epoch: {} [{}/{} ({:.0f}%)]\t SoftCdet: {:.6f}'.format(
                epoch, batch_idx * len(data1), len(train_loader.dataset),
                       100. * batch_idx / len(train_loader), np.mean(softcdets)))
#            print('Train Epoch: {} [{}/{} ({:.0f}%)]\t Crossentropy: {:.6f}'.format(
#                epoch, batch_idx * len(data1), len(train_loader.dataset),
#                       100. * batch_idx / len(train_loader), np.mean(crossentropies)))
#            logging.info('Train Epoch: {} [{}/{} ({:.0f}%)]\t Crossentropy: {:.6f}'.format(
#                epoch, batch_idx * len(data1), len(train_loader.dataset),
#                       100. * batch_idx / len(train_loader), np.mean(crossentropies)))
            Pmiss1 = miss1 / tgt_count
            Pfa1 = fa1 / non_tgt_count
            nbatchCdet1 = Pmiss1 + 99 * Pfa1
            Pmiss2 = miss2 / tgt_count
            Pfa2 = fa2 / non_tgt_count
            nbatchCdet2 = Pmiss2 + 199 * Pfa2
            nbatchCdet = (nbatchCdet1 + nbatchCdet2) / 2
            fa1 = 0
            miss1 = 0
            fa2 = 0
            miss2 = 0
            tgt_count = 0
            non_tgt_count = 0
            softcdets = []
#            crossentropies = []
            model.eval()
            minC_threshold1, minC_threshold2, min_cent_threshold = compute_minc_threshold(args, model, device,
                                                                                          mega_xvec_dict,
                                                                                          num_to_id_dict, valid_loader)
            model.train()


def validate(args, model, device, mega_xvec_dict, num_to_id_dict, data_loader):
    model.eval()
    minC_threshold1, minC_threshold2, min_cent_threshold = compute_minc_threshold(args, model, device, mega_xvec_dict,
                                                                                  num_to_id_dict, data_loader)
    test_loss = 0
    correct = 0
    fa1 = 0
    miss1 = 0
    fa2 = 0
    miss2 = 0
    tgt_count = 0
    non_tgt_count = 0
    with torch.no_grad():
        for data1, data2, target in data_loader:
            data1, data2, target = data1.to(device), data2.to(device), target.to(device)
            data1_xvec, data2_xvec = load_xvec_from_batch(mega_xvec_dict, num_to_id_dict, data1, data2,
                                                          device)  # mega_xvec_dict[num_to_id_dict[data1]], mega_xvec_dict[num_to_id_dict[data2]]
            output = model(data1_xvec, data2_xvec)
            sigmoid = nn.Sigmoid()
            test_loss += F.binary_cross_entropy(sigmoid(output - min_cent_threshold), target).item()
            correct_preds = (((output - min_cent_threshold) > 0).float() == target).float()
            correct += (correct_preds).sum().item()
            tgt_count += target.sum().item()
            non_tgt_count += (1 - target).sum().item()
            fa1 += ((output > minC_threshold1).float() * (1 - target)).sum().item()
            miss1 += ((output < minC_threshold1).float() * target).sum().item()
            fa2 += ((output > minC_threshold2).float() * (1 - target)).sum().item()
            miss2 += ((output < minC_threshold2).float() * target).sum().item()
    Pmiss1 = miss1 / tgt_count
    Pfa1 = fa1 / non_tgt_count
    Cdet1 = Pmiss1 + 99 * Pfa1
    Pmiss2 = miss2 / tgt_count
    Pfa2 = fa2 / non_tgt_count
    Cdet2 = Pmiss2 + 199 * Pfa2
    Cdet = (Cdet1 + Cdet2) / 2
    test_loss /= len(data_loader.dataset)
    print('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.1f}%)\n'.format(
        test_loss, correct, len(data_loader.dataset),
        100. * correct / len(data_loader.dataset)))
    print('\nTest set: Pfa1: {:.4f}\n'.format(Pfa1))
    print('\nTest set: Pmiss1: {:.4f}\n'.format(Pmiss1))
    print('\nTest set: Pfa2: {:.4f}\n'.format(Pfa2))
    print('\nTest set: Pmiss2: {:.4f}\n'.format(Pmiss2))
    print('\nTest set: C_det(149): {:.4f}\n'.format(Cdet))

    logging.info('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.1f}%)\n'.format(
        test_loss, correct, len(data_loader.dataset),
        100. * correct / len(data_loader.dataset)))
    logging.info('\nTest set: Pfa1: {:.2f}\n'.format(Pfa1))
    logging.info('\nTest set: Pmiss1: {:.2f}\n'.format(Pmiss1))
    logging.info('\nTest set: Pfa2: {:.2f}\n'.format(Pfa2))
    logging.info('\nTest set: Pmiss2: {:.2f}\n'.format(Pmiss2))
    logging.info('\nTest set: C_det(149): {:.2f}\n'.format(Cdet))
    return Cdet, minC_threshold1, minC_threshold2, min_cent_threshold


def compute_minc_threshold(args, model, device, mega_xvec_dict, num_to_id_dict, data_loader):
    device1 = torch.device('cpu')
    model = model.to(device1)
    with torch.no_grad():
        targets, scores = np.asarray([]), np.asarray([])
        for data1, data2, target in data_loader:
            data1, data2, target = data1.to(device1), data2.to(device1), target.to(device1)
            data1_xvec, data2_xvec = load_xvec_from_batch(mega_xvec_dict, num_to_id_dict, data1, data2, device1)
            targets = np.concatenate((targets, np.asarray(target)))
            scores = np.concatenate((scores, np.asarray(model.forward(data1_xvec, data2_xvec))))
    minC_threshold1, minC_threshold2, min_cent_threshold = get_cmn2_thresholds(scores, targets)
    model = model.to(device)
    return minC_threshold1, minC_threshold2, min_cent_threshold


def score_18_eval(sre18_eval_trials_file_path, model, device, sre18_eval_xv_pairs_1, sre18_eval_xv_pairs_2):
    generate_scores_in_batches("scores/{}_{}.txt".format('sre18_eval', timestamp), device, sre18_eval_trials_file_path,
                               sre18_eval_xv_pairs_1, sre18_eval_xv_pairs_2, model)


def main_score_eval():
    print("Scoring eval")
    device = torch.device('cuda')
    model = pickle.load(
        open('/home/data2/SRE2019/shreyasr/X/models/kaldi_pldaNet_sre0410_swbd_16_10.swbdsremx6epoch.1571810057.pt',
             'rb'))
    model = model.to(device)
    sre18_eval_trials_file_path = "/home/data/SRE2019/LDC2019E59/eval/docs/sre18_eval_trials.tsv"
    trial_file_path = "/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre18_eval_test/trials"
    enroll_spk2utt_path = "/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre18_eval_enrollment/spk2utt"
    enroll_xvector_path = "/home/data2/SRE2019/prashantk/voxceleb/v2/exp/xvector_nnet_1a/xvectors_sre18_eval_enrollment/xvectors.pkl"
    test_xvector_path = "/home/data2/SRE2019/prashantk/voxceleb/v2/exp/xvector_nnet_1a/xvectors_sre18_eval_test/xvectors.pkl"
    enroll_spk2xvectors = get_spk2xvector(enroll_spk2utt_path, enroll_xvector_path)
    test_xvectors = pickle.load(open(test_xvector_path, 'rb'))
    sre18_eval_xv_pairs_1, sre18_eval_xv_pairs_2 = xv_pairs_from_trial(trial_file_path, enroll_spk2xvectors,
                                                                       test_xvectors)
    score_18_eval(sre18_eval_trials_file_path, model, device, sre18_eval_xv_pairs_1, sre18_eval_xv_pairs_2)
    print("Done")


def main_kaldiplda():
    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch MNIST Example')
    parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                        help='input batch size for testing (default: 1000)')
    parser.add_argument('--epochs', type=int, default=30, metavar='N',
                        help='number of epochs to train (default: 10)')
    parser.add_argument('--lr', type=float, default=0.0001, metavar='LR',
                        help='learning rate (default: 0.001)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--log-interval', type=int, default=100, metavar='N',
                        help='how many batches to wait before logging training status')

    parser.add_argument('--save-model', action='store_true', default=True,
                        help='For Saving the current Model')
    args = parser.parse_args()
    use_cuda = not args.no_cuda and torch.cuda.is_available()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)

    logging.info("Started at {}\n\n softCdet Loss. Random init. Batch size = 4096. Threshold1 and Threshold2 fixed using SRE 2018 dev after kaldi init. \n\n".format(
            datetime.now())) #bce1 = (target*torch.log(sigmoid(model.threshold1 - output))).sum() + 99*((1 - target)*torch.log(1-sigmoid(model.threshold1 - output))).sum() \n bce2 = (target*torch.log(sigmoid(model.threshold2 - output))).sum() + 199*((1 - target)*torch.log(1-sigmoid(model.threshold2 - output))).sum()\nloss = 0.5 * (bce1 + bce2) 

    device = torch.device("cuda" if use_cuda else "cpu")

    ###########################################################################
    # Generating training data loaders here
    ###########################################################################

    datasets_train = []
    datasets_valid = []
    mega_xvec_dict = pickle.load(open('pickled_files/mega_xvec_dict_xvectors.pkl', 'rb'))
    num_to_id_dict = {i: j for i, j in enumerate(list(mega_xvec_dict))}
    id_to_num_dict = {v: k for k, v in num_to_id_dict.items()}

    data_dir_list = np.asarray([['/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre2004/male', '5'],
                                ['/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre2004/female', '5'],
                                ['/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre_2005_2006_08/male', '7'],
                                ['/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre_2005_2006_08/female', '7'],
                                ['/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre10/male', '10'],
                                ['/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre10/female', '10'],
                                ['/home/data2/SRE2019/prashantk/voxceleb/v3/data/swbd/male', '2'],
                                ['/home/data2/SRE2019/prashantk/voxceleb/v3/data/swbd/female', '2'],
                                ['/home/data2/SRE2019/prashantk/voxceleb/v3/data/mx6/grepd/male', '4'],
                                ['/home/data2/SRE2019/prashantk/voxceleb/v3/data/mx6/grepd/female', '5']])

    xvector_scp_list = np.asarray(
        ['/home/data2/SRE2019/prashantk/voxceleb/v2/exp/xvector_nnet_1a/xvectors_swbd/xvector_fullpaths.scp',
         '/home/data2/SRE2019/prashantk/voxceleb/v2/exp/xvector_nnet_1a/xvectors_sre/xvector_fullpaths.scp',
         '/home/data2/SRE2019/prashantk/voxceleb/v2/exp/xvector_nnet_1a/xvectors_mx6/xvector_fullpaths.scp'])


    train_set, val_set = get_train_dataset(data_dir_list, xvector_scp_list, id_to_num_dict, batch_size=4096, train_and_valid=True, train_ratio=0.95)
    datasets_train.append(train_set)
    datasets_valid.append(val_set)

    # NOTE: 'xvectors.pkl' files are generated using utils/Kaldi2NumpyUtils/kaldivec2numpydict.py

    trial_file_path = "/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre18_dev_test/trials"
    enroll_spk2utt_path = "/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre18_dev_enrollment/spk2utt"
    enroll_xvector_path = "/home/data2/SRE2019/prashantk/voxceleb/v2/exp/xvector_nnet_1a/xvectors_sre18_dev_enrollment/xvectors.pkl"
    test_xvector_path = "/home/data2/SRE2019/prashantk/voxceleb/v2/exp/xvector_nnet_1a/xvectors_sre18_dev_test/xvectors.pkl"
    enroll_spk2xvector = get_spk2xvector(enroll_spk2utt_path, enroll_xvector_path)
    test_xvectors = pickle.load(open(test_xvector_path, 'rb'))
    #    mega_xvec_dict.update(enroll_spk2xvector)
    #    mega_xvec_dict.update(test_xvectors)
    sre18_dev_trials_loader = dataloader_from_trial(trial_file_path, id_to_num_dict, batch_size=4096, shuffle=True)
    sre18_dev_xv_pairs_1, sre18_dev_xv_pairs_2 = xv_pairs_from_trial(trial_file_path, enroll_spk2xvector, test_xvectors)

    trial_file_path = "/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre16_eval_test/trials"
    #    enroll_spk2utt_path="/home/data2/SRE2019/prashantk/voxceleb/v3/data/sre16_eval_enrollment/spk2utt"
    #    enroll_xvector_path="/home/data2/SRE2019/prashantk/voxceleb/v2/exp/xvector_nnet_1a/xvectors_sre16_eval_enrollment/xvectors.pkl"
    #    test_xvector_path="/home/data2/SRE2019/prashantk/voxceleb/v2/exp/xvector_nnet_1a/xvectors_sre16_eval_test/xvectors.pkl"
    #    enroll_spk2xvector = get_spk2xvector(enroll_spk2utt_path,enroll_xvector_path)
    #    test_xvectors = pickle.load(open(test_xvector_path,'rb'))
    #    mega_xvec_dict.update(enroll_spk2xvector)
    #    mega_xvec_dict.update(test_xvectors)
    sre16_eval_trials_train_dataset, sre16_eval_trials_valid_dataset = dataset_from_trial(trial_file_path, id_to_num_dict, batch_size=4096, shuffle=True, train_and_valid=True, train_ratio=0.95)
    datasets_train.append(sre16_eval_trials_train_dataset)
    datasets_valid.append(sre16_eval_trials_valid_dataset)

    trials_08, enroll_xvectors_08, enroll_model2xvector_08, all_utts_dict_08 = get_sre08_trials_etc()
    sre08_train_dataset, sre08_valid_dataset = dataset_from_sre08_10_trial(trials_08, id_to_num_dict, all_utts_dict_08, batch_size=4096, shuffle=True, train_and_valid=True, train_ratio=0.95)
    #    mega_xvec_dict.update(enroll_model2xvector_08)
    #    mega_xvec_dict.update(enroll_xvectors_08)
    datasets_train.append(sre08_train_dataset)
    datasets_valid.append((sre08_valid_dataset))

    trials_10, enroll_xvectors_10, enroll_model2xvector_10, all_utts_dict_10 = get_sre10_trials_etc()
    sre10_train_dataset, sre10_valid_dataset = dataset_from_sre08_10_trial(trials_10, id_to_num_dict, all_utts_dict_10, batch_size=4096, shuffle=True, train_and_valid=True, train_ratio=0.95)
    #    mega_xvec_dict.update(enroll_model2xvector_10)
    #    mega_xvec_dict.update(enroll_xvectors_10)
    datasets_train.append(sre10_train_dataset)
    datasets_valid.append(sre10_valid_dataset)

    #    pickle.dump(mega_xvec_dict, open('pickled_files/mega_xvec_dict.pkl','wb'))

    
    combined_dataset_valid = torch.utils.data.ConcatDataset(datasets_valid)
    
#     val_loader = torch.utils.data.DataLoader(combined_dataset_valid, batch_size=len(combined_dataset_valid))
#     for a,b,c in val_loader:
#         enr,test,labels=a,b,c
#     trials = (np.c_[enr,test,labels]).astype(int)
#     dev_utts = np.unique(trials[:,:2].ravel())
#     trials = trials.astype(int).astype(str).astype('<U29')
#     tgt = {'1':'target', '0':'nontarget'}
#     bp()
#     for a in trials:
#         a[0],a[1],a[2] = num_to_id_dict[int(a[0])],num_to_id_dict[int(a[1])],tgt[a[2]]
#     xvec_txt = np.asarray([re.sub(' +',' ',"{} {}".format(num_to_id_dict[i],mega_xvec_dict[num_to_id_dict[i]]).replace('[','[ ').replace(']',' ]').replace('\n',' ')) for i in dev_utts])
# #    bp()
#     np.savetxt('valid_trials_new',trials,fmt='%s',delimiter=' ',comments='')
#     np.savetxt('valid_xvector_new.txt',xvec_txt,fmt='%s',delimiter=' ',comments='')


    train_loader = concatenate_datasets(datasets_train, batch_size=4096)
    valid_loader = concatenate_datasets(datasets_valid, batch_size=4096)
    

    ###########################################################################
    # Fishished generating training data loaders
    ###########################################################################

    model = NeuralPlda().to(device)
    ## Uncomment to initialize with a pickled pretrained model or a Kaldi PLDA model 

    # model = pickle.load(open('/home/data2/SRE2019/shreyasr/X/models/kaldi_pldaNet_sre0410_swbd_16_16.swbdsremx6epoch.1571651491.pt','rb'))

    ## To load a Kaldi trained PLDA model, Specify the paths of 'mean.vec', 'transform.mat' and 'plda' generated from stage 8 of https://github.com/kaldi-asr/kaldi/blob/master/egs/sre16/v2/run.sh 

    model.LoadPldaParamsFromKaldi('Kaldi_Models/mean.vec', 'Kaldi_Models/transform.mat', 'Kaldi_Models/plda')


    sre18_dev_trials_file_path = "/home/data/SRE2019/LDC2019E59/dev/docs/sre18_dev_trials.tsv"
    lr = args.lr
    optimizer = optim.Adam(model.parameters(), lr=lr)
    all_losses = []

    bestloss = 1000

    print("Validation Set:")
    logging.info("Validation Set Trials:")

    valloss, minC_threshold1, minC_threshold2, min_cent_threshold = validate(args, model, device, mega_xvec_dict, num_to_id_dict, valid_loader)
#    model.state_dict()['threshold1'].data.copy_(torch.tensor([float(minC_threshold1)]).float())
#    model.state_dict()['threshold2'].data.copy_(torch.tensor([float(minC_threshold2)]).float())
    # model.threshold1 = torch.tensor([float(minC_threshold1)]).float().to(device)
    # model.threshold2 = torch.tensor([float(minC_threshold2)]).float().to(device)
    all_losses.append(valloss)

    print("SRE18_Dev Trials:")
    logging.info("SRE18_Dev Trials:")
    # bp()
    valloss, minC_threshold1, minC_threshold2, min_cent_threshold = validate(args, model, device, mega_xvec_dict, num_to_id_dict, sre18_dev_trials_loader)
    #    model.state_dict()['threshold1'].data.copy_(torch.tensor([float(minC_threshold1)]).float())
    #    model.state_dict()['threshold2'].data.copy_(torch.tensor([float(minC_threshold2)]).float())
    # model.threshold1 = torch.tensor([float(minC_threshold1)]).float().to(device)
    # model.threshold2 = torch.tensor([float(minC_threshold2)]).float().to(device)
    # all_losses.append(valloss)

    for epoch in range(1, args.epochs + 1):
        train(args, model, device, train_loader, valid_loader, mega_xvec_dict, num_to_id_dict, optimizer,
              epoch)
        print("Validataion Set Trials:")
        logging.info("Validation Set Trials:")

        valloss, minC_threshold1, minC_threshold2, min_cent_threshold = validate(args, model, device, mega_xvec_dict,
                                                                                 num_to_id_dict,
                                                                                 valid_loader)
#        if epoch%1 == 0:
#            model.state_dict()['threshold1'].data.copy_(torch.tensor([float(minC_threshold1)]).float())
#            model.state_dict()['threshold2'].data.copy_(torch.tensor([float(minC_threshold2)]).float())
        all_losses.append(valloss)

        print("SRE16_18_dev_eval Trials:")
        logging.info("SRE16_18_dev_eval Trials:")
        valloss, minC_threshold1, minC_threshold2, min_cent_threshold = validate(args, model, device, mega_xvec_dict,
                                                                                 num_to_id_dict,
                                                                                 sre18_dev_trials_loader)
        #        if epoch%5 == 0:
        #            model.state_dict()['threshold1'].data.copy_(torch.tensor([float(minC_threshold1)]).float())
        #            model.state_dict()['threshold2'].data.copy_(torch.tensor([float(minC_threshold2)]).float())

        # all_losses.append(valloss)
        model.SaveModel("models/kaldi_pldaNet_sre0410_swbd_16_{}.swbdsremx6epoch.{}.pt".format(epoch, timestamp))
        print("Generating scores for Epoch ", epoch)
        generate_scores_in_batches("scores/scores_kaldipldanet_CUDA_Random{}_{}.txt".format(epoch, timestamp), device,
                                   sre18_dev_trials_file_path, sre18_dev_xv_pairs_1, sre18_dev_xv_pairs_2, model)

        try:
            if all_losses[-1] < bestloss:
                bestloss = all_losses[-1]
            if (all_losses[-1] > all_losses[-2]) and (all_losses[-2] > all_losses[-3]):
                lr = lr / 2
                print("REDUCING LEARNING RATE to {} since loss trend looks like {}".format(lr, all_losses[-3:]))
                logging.info("REDUCING LEARNING RATE to {} since loss trend looks like {}".format(lr, all_losses[-3:]))
                optimizer = optim.Adam(model.parameters(), lr=lr)
        except:
            pass


if __name__ == '__main__':
    main_kaldiplda()
    # generate_scores_from_plda('/home/data2/shreyasr/NeuralPlda/Kaldi_Models/mean.vec',
    #                           '/home/data2/shreyasr/NeuralPlda/Kaldi_Models/transform.mat',
    #                           '/home/data2/shreyasr/NeuralPlda/Kaldi_Models/plda', 'scoring_valid_set_2',
    #                           '/home/data2/shreyasr/NeuralPlda/valid_trials_new',
    #                           '/home/data2/shreyasr/NeuralPlda/valid_xvector.scp',
    #                           '/home/data2/shreyasr/NeuralPlda/valid_spk2utt',
    #                           '/home/data2/shreyasr/NeuralPlda/num_utts.ark')

#    main_score_eval()
#    finetune('models/kaldi_pldaNet_sre0410_swbd_16_1.swbdsremx6epoch.1571827115.pt')
