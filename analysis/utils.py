#!/usr/bin/env python3

"""
The graph_constructor module constructs the input graphs to the ML analysis:
    - graphs_numpy_subjet.h5: builds graphs from JFN output subjets_unshuffled.h5
    - graphs_pyg_subjet__{graph_key}.pt: builds PyG graphs from subjet_graphs_numpy.h5
    - graphs_pyg_particle__{graph_key}.pt: builds PyG graphs from energyflow dataset
"""

import os
import sys
import tqdm
import yaml
import numpy as np
import numba

import energyflow as ef

import torch
import torch_geometric
#from torch_geometric.data import Data, Dataset

import h5py as h5
import json, time
from sklearn.utils import shuffle
import time

def get_mjj_mask(mjj,use_SR=False,mjjmin=2300,mjjmax=5000):
    if use_SR:
        mask_region = (mjj>=3300) & (mjj<=3700)
    else:
        mask_region = ((mjj<=3300) & (mjj>=mjjmin)) | ((mjj>=3700) & (mjj<=mjjmax))
    return mask_region

def revert_mjj(mjj,mjjmin=2300,mjjmax=5000):
    x = (mjj + 1.0)/2.0
    logmin = np.log(mjjmin)
    logmax = np.log(mjjmax)
    x = x * ( logmax - logmin ) + logmin
    return np.exp(x)

def prep_mjj(mjj,mjjmin=2300,mjjmax=5000):
    new_mjj = (np.log(mjj) - np.log(mjjmin))/(np.log(mjjmax) - np.log(mjjmin))
    new_mjj = 2*new_mjj -1.0
    return new_mjj



def LoadJson(file_name):
    import json,yaml
    JSONPATH = os.path.join(file_name)
    return yaml.safe_load(open(JSONPATH))

def SaveJson(save_file,data):
    with open(save_file,'w') as f:
        json.dump(data, f)


def _preprocessing(particles,jets,mjj,save_json=False, norm = 'mean'):
    n_part = particles.shape[2]
    batch_size = particles.shape[0]

    jets[:,:,0] = jets[:,:,0]/np.expand_dims(mjj,-1)
    jets[:,:,3] = jets[:,:,3]/np.expand_dims(mjj,-1)
        
    particles=particles.reshape(-1,particles.shape[-1]) #flatten
    jets=jets.reshape(-1,jets.shape[-1]) #flatten

        
    #Transformations
    particles[:,0] = np.ma.log(1.0 - particles[:,0]).filled(0)
    jets[:,0] = np.log(jets[:,0])
    jets[:,3] = np.ma.log(jets[:,3]).filled(0)

        
    if save_json:
        mask = particles[:,-1]
        mean_particle = np.average(particles[:,:-1],axis=0,weights=mask)
        std_particle = np.sqrt(np.average((particles[:,:-1] - mean_particle)**2,axis=0,weights=mask))
        data_dict = {
                'max_jet':np.max(jets,0).tolist(),
                'min_jet':np.min(jets,0).tolist(),
                'max_particle':np.max(particles[:,:-1],0).tolist(),
                'min_particle':np.min(particles[:,:-1],0).tolist(),
                'mean_jet': np.mean(jets,0).tolist(),
                'std_jet': np.std(jets,0).tolist(),
                'mean_particle': mean_particle.tolist(),
                'std_particle': std_particle.tolist(),                     
                }                
            
        SaveJson('preprocessing_{}.json'.format(n_part),data_dict)
    else:
        data_dict = LoadJson('preprocessing_{}.json'.format(n_part))

            
    if norm == 'mean':
        jets = np.ma.divide(jets-data_dict['mean_jet'],data_dict['std_jet']).filled(0)
        particles[:,:-1]= np.ma.divide(particles[:,:-1]-data_dict['mean_particle'],data_dict['std_particle']).filled(0)
    elif norm == 'min':
        jets = np.ma.divide(jets-data_dict['min_jet'],np.array(data_dict['max_jet']) -data_dict['min_jet']).filled(0)
        particles[:,:-1]= np.ma.divide(particles[:,:-1]-data_dict['min_particle'],np.array(data_dict['max_particle']) - data_dict['min_particle']).filled(0)            
    else:
        print("ERROR: give a normalization method!")
    particles = particles.reshape(batch_size,2,n_part,-1)
    jets = jets.reshape(batch_size,2,-1)
    return particles.astype(np.float32),jets.astype(np.float32)



def SimpleLoader(data_path,file_name,use_SR=False,
                 npart=100,mjjmin=2300,mjjmax=5000):


    with h5.File(os.path.join(data_path,file_name),"r") as h5f:
        particles = h5f['constituents'][:]
        jets = h5f['jet_data'][:]
        mask = h5f['mask'][:]
        particles = np.concatenate([particles,mask],-1)

    p4_jets = ef.p4s_from_ptyphims(jets)
    # get mjj from p4_jets
    sum_p4 = p4_jets[:, 0] + p4_jets[:, 1]
    mjj = ef.ms_from_p4s(sum_p4)
    jets = np.concatenate([jets,np.sum(mask,-2)],-1)    

    # train using only the sidebands

    mask_region = get_mjj_mask(mjj,use_SR,mjjmin,mjjmax)
    mask_mass = (np.abs(jets[:,0,0])>0.0) & (np.abs(jets[:,1,0])>0.0)
    
    particles = particles[(mask_region) & (mask_mass)]
    mjj = mjj[(mask_region) & (mask_mass)]
    jets = jets[(mask_region) & (mask_mass)]
    jets[:,:,-1][jets[:,:,-1]<0] = 0.
    
    if not use_SR: # should nt this be the 10% not used for training in order to have a fair comparison?
        #Load validation split
        particles = particles[:int(0.9*jets.shape[0])]
        mask = mask[:int(0.9*jets.shape[0])]
        jets = jets[:int(0.9*jets.shape[0])]
    
    
    mask = np.expand_dims(particles[:,:,:,-1],-1)
    return particles[:,:,:,:-1]*mask,jets,mjj



def class_loader(n_events,
                 data_path='/pscratch/sd/d/dimathan/LHCO/Data', 
                 file_name='processed_data_background_rel.h5' , # the default file name for the background data
                 npart=279,
                 use_SR=True,
                 nsig=15000,
                 nbkg=60671,
                 mjjmin=2300,
                 mjjmax=5000):
    

    parts_bkg, jets_bkg, mjj_bkg = SimpleLoader(data_path, file_name, use_SR=use_SR, npart=npart)

    parts_bkg = parts_bkg[:nbkg]
    mjj_bkg = mjj_bkg[:nbkg]
    jets_bkg = jets_bkg[:nbkg]

    if nsig>0:
        parts_sig,jets_sig,mjj_sig = SimpleLoader(data_path, 'processed_data_signal_rel.h5', use_SR=use_SR, npart=npart)
        parts_sig = parts_sig[:nsig]
        mjj_sig = mjj_sig[:nsig]
        jets_sig = jets_sig[:nsig]

        labels = np.concatenate([np.zeros_like(mjj_bkg), np.ones_like(mjj_sig)])
        particles = np.concatenate([parts_bkg, parts_sig],0)
        jets = np.concatenate([jets_bkg, jets_sig],0)
        mjj = np.concatenate([mjj_bkg, mjj_sig],0)
    else:
        labels = np.zeros_like(mjj_bkg)
        particles = parts_bkg
        jets = jets_bkg
        mjj = mjj_bkg

    return jets, particles, mjj, labels



def DataLoader(n_events,
               data_path='/pscratch/sd/d/dimathan/LHCO/Data', 
               file_name='processed_data_background_rel.h5' , # the default file name for the background data
               npart=279,
               n_events_sample = 500,
               ddp = False,
               rank=0,size=1,
               batch_size=64,
               make_torch_data=True,
               use_SR=False,
               norm = None,
               mjjmin=2300,
               mjjmax=5000,):

    with h5.File(os.path.join(data_path,file_name),"r") as h5f:
        nevts = min(n_events, h5f['jet_data'][:].shape[0])          # number of events
        particles = h5f['constituents'][:nevts]
        jets = h5f['jet_data'][:nevts]
        mask = h5f['mask'][:nevts]
        particles = np.concatenate([particles,mask],-1)

    # print the analytics of the data
    if rank == 0:
        print(f'nevts: {nevts}')
        print(f'Particles shape: {particles.shape}')
        print(f'Jets shape: {jets.shape}')
        print()


    p4_jets = ef.p4s_from_ptyphims(jets)
    # get mjj from p4_jets
    sum_p4 = p4_jets[:, 0] + p4_jets[:, 1]
    mjj = ef.ms_from_p4s(sum_p4)
    jets = np.concatenate([jets,np.sum(mask,-2)],-1)

    # train using only the sidebands
    mask_region = get_mjj_mask(mjj,use_SR=False)              # Only events in the sideband region (~80% of the events)
    mask_mass = (np.abs(jets[:,0,0])>0.0) & (np.abs(jets[:,1,0])>0.0) # Both jet have non-zero p_T (not zero padded events)
  
    #how many events outside mask_region and mask_mass
    if rank == 0:
        print(f'Number of events outside mask_region: {np.sum(~mask_region)}')
        print()
        print(f'Number of events inside mask_region: {np.sum(mask_region)}')
        print()


    particles = particles[(mask_region) & (mask_mass)]
    mjj = mjj[(mask_region) & (mask_mass)]
    jets = jets[(mask_region) & (mask_mass)]

    jets[:,:,-1][jets[:,:,-1]<0] = 0.

    # should we preprocess the data before constructing the graphs?
    #particles, jets = _preprocessing(particles, jets, mjj)
    #mjj = prep_mjj(mjj, mjjmin, mjjmax)

    return particles, jets, mjj



#---------------------------------------------------------------
# Construct graphs from input_data and write them to file
#---------------------------------------------------------------
def construct_graphs(output_dir, use_precomputed_graphs=False, sub_or_part='particle', graph_structure='fully_connected', n_events=100000):
    '''
    Construct graphs:
      - Particle graphs are constructed from energyflow dataset
      - Subjet graphs are constructed from JFN dataset

    Several graph structures are generated:
      - Subjet graphs: Fully connected, Laman graphs (naive, 1N, 1N2N)
      - Particle graphs: Fully connected

    There are several different feature constructions as well:
      - Node features: 
          - Subjet graphs: (z)
          - Particle graphs: (z,y,phi)
      - Edge features:
          - Subjet graphs: pairwise angles
          - Particle graphs: no edge features
    TODO: implement more comprehensive options

    The graphs are saved in several formats:
      - graphs_numpy_subjet.h5: numpy arrays
      - graphs_pyg_subjet__{graph_key}.pt: PyG data objects
      - graphs_pyg_particle__{graph_key}.pt: PyG data objects
    '''
    t_st = time.time()
    # PyG format
    _construct_particle_graphs_pyg(output_dir, graph_structure, n_events=n_events, use_SR=False)
    _construct_particle_graphs_pyg(output_dir, graph_structure, n_events=n_events, use_SR=True)

    print(f'Finished constructing graphs in {time.time() - t_st:.2f} seconds.')
    



#---------------------------------------------------------------
# Construct graphs from input_data and write them to file
#---------------------------------------------------------------
def _construct_particle_graphs_pyg(output_dir, graph_structure, n_events=500000, rank=0, use_SR=False):
    '''
    Construct a list of PyG graphs for the particle-based GNNs, loading from the energyflow dataset

    Graph structure:
        - Nodes: particle four-vectors
        - Edges: no edge features
        - Connectivity: fully connected (TODO: implement other connectivities)
    '''
    print(f'Constructing PyG particle graphs from energyflow dataset...')

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # Load data
    if not use_SR:
        particles, jets, mjj = DataLoader(n_events=n_events, rank=rank)
        labels = [0]*len(particles)
    else:   
        particles, jets, mjj, labels = class_loader(n_events=1000000, use_SR=True, nsig = 2500)

    # preprocess the data before constructing the fully connected graphs 
    particles, jets = _preprocessing(particles, jets, mjj, save_json=True, norm = 'mean')

    if use_SR:
        graph_key = f'SR__{graph_structure}'
    else:
        graph_key = f'SB__{graph_structure}'

    graph_list = []
    n_events = particles.shape[0]
    n_part = particles.shape[2]

    # Convert entire dataset to GPU once (only if it fits into GPU memory and you really need to do this now)
    #particles_t = torch.tensor(particles, dtype=torch.float, device=device)
    #jets_t = torch.tensor(jets, dtype=torch.float, device=device)
    #mjj_t = torch.tensor(mjj, dtype=torch.float, device=device)

    # Precompute edge_index once
    edge_pairs = [[i, j] for i in range(n_part) for j in range(n_part) if i != j]
    edge_index = torch.tensor(edge_pairs, dtype=torch.long).t().contiguous().to(device)

    for event_idx in range(particles.shape[0]):
        event_graphs  = []
        event_label = labels[event_idx] 
        for jet_idx in range(2):  # Two jets per event
            # Get particle features for the current jet
            jet_particles = particles[event_idx, jet_idx][:, :3]  # Shape: (num_particles, pt, eta, phi)

            # Node features
            x = torch.tensor(jet_particles, dtype=torch.float).to(device)  # Shape: (n_part, features)
            # Global features (optional)
            #u = torch.tensor([mjj[event_idx], *jets[event_idx, jet_idx]], dtype=torch.float).to(device)

            # Create PyG Data object
            data = torch_geometric.data.Data(x=x, edge_index=edge_index)
            data.y = torch.tensor([event_label], dtype=torch.long).to(device) # Add label, 0 if bkg, 1 if signal

            event_graphs.append(data)

        graph_list.append(event_graphs)


    # Save to file using pytorch
    graph_filename = os.path.join(output_dir, f"graphs_pyg_{graph_key}.pt")
    # Ensure the parent directory exists
    os.makedirs(os.path.dirname(graph_filename), exist_ok=True)

    torch.save(graph_list, graph_filename)
    print(f'Saved PyG graphs to {graph_filename}.')

