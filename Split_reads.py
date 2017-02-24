"""
Akshay Tambe
Pachter and Doudna groups


Split reads for dropseq data, optionally run kallisto and produce TCCs
1. Index kmers
		Produce a dict kmer_index 
		where kmer_index[kmer] -> list of read line numbers that contain this kmer
2. Find cyclic paths
		pick a popular kmer
		get all reads that contain the kmer
		make subgraph from that subset of reads
		get best path(s) starting at the starting kmer

3. Threshold paths 
		Histogram of path weights has a minimum

4. Assign reads
		For each read, find the path that it shares the most kmers with
"""

import IO_utils
import argparse
import os
import sys
import time
import subprocess
import json
import collections
import itertools
import gc
import numpy as np


from Graph_utils import Edge, Graph, Path
from multiprocessing import Pool 

 
args = {}

def run_all(cmdline_args):
	global args
	args = cmdline_args
	print('Running Dropseq_subgraphs\nArgs:%s' % \
		json.dumps(args, indent = 5))
	
	start_time = time.time()

	output_files = {}
	output_dir = args['output_dir']
	output_files['log'] = '%s/run_log.txt' % output_dir
	sys.stdout = IO_utils.Logger(output_files['log'])
	reads_unzipped = args['reads']
	barcodes_unzipped = args['barcodes']
	
	print('Initializing redis-db server')
	kmer_idx_db, kmer_idx_pipe = IO_utils.initialize_redis_pipeline(db=0)
	print('Indexing reads by circularized kmers')
	kmer_idx_pipe = get_kmer_index_db(
		(kmer_idx_pipe, barcodes_unzipped, reads_unzipped))
	kmer_counts = get_kmer_counts(kmer_idx_db, kmer_idx_pipe)
	print('\t%i kmers indexed' % len(kmer_counts.items()))
	
	print('Finding cyclic paths')
	cyclic_paths = find_paths(
		(kmer_idx_pipe,
		kmer_counts,
		barcodes_unzipped, 
		reads_unzipped))	
	print('\t%i cyclic paths found' % len(cyclic_paths))
	output_files['all_paths'] = IO_utils.save_paths_text(
		output_dir, cyclic_paths, prefix='all')		
	print('Merging similar paths')
	merged_paths = merge_paths(cyclic_paths)
	output_files['merged_paths'] = IO_utils.save_paths_text(
		output_dir, merged_paths, prefix='merged')	
	print('\t%i paths remaining after merging' % len(merged_paths))
	
	print('Thresholding merged paths')
	(threshold, top_paths, fit_out) = threshold_paths(output_dir, merged_paths)
	output_files.update(fit_out)
	print('\tUsing threshold at depth. Threshold is %i' % threshold)
	print('\t%i paths have weight higher than the threshold' % len(top_paths))
	
	kmer_idx_db.flushall()
	print('Assigning reads')
	reads_assigned_db, reads_assigned_pipe = assign_all_reads(
		top_paths, reads_unzipped, barcodes_unzipped)
	
	print('Splitting reads by cell')
	output_files['split'] = write_split_fastqs(
		(reads_assigned_db,
		reads_assigned_pipe,
		output_dir,
		reads_unzipped,
		barcodes_unzipped))
	
	print('Flushing assignments index')
	reads_assigned_db.flushall()
	
	current_time = time.time()
	elapsed_time = current_time - start_time
	return(output_files, elapsed_time)
	
def get_kmer_index_db(params):
	MAX_READS_TO_INDEX = 5000000
	
	(	kmer_idx_pipe,
		barcodes_unzipped, 
		reads_unzipped) = params
	length = args['barcode_end'] - args['barcode_start']
	pool = Pool(processes = args['threads'])
	
	read_count = 0
	for reads_chunk in IO_utils.get_read_chunks(
		args,
		barcodes_unzipped,
		reads_unzipped,
		lines=None):
		
		read_count += len(reads_chunk)
		chunk_kmer_indices = pool.map(
			index_read,
			reads_chunk)
		#chunk_kmer_indices is a list of dicts
		for element in chunk_kmer_indices:
			for(key, offsets) in element.items():
				#value is a list: [offset1, offset2, offset3 ...]
				value = ','.join(['%i' % i for i in offsets]) + ','
				kmer_idx_pipe.append(key.encode('utf-8'), value.encode('utf-8'))
		pipe_out = kmer_idx_pipe.execute()#slow. chunks are large to minimize this
		del(chunk_kmer_indices); _ = gc.collect()
		print('\t%i reads indexed' % read_count)
		
		if(read_count >= MAX_READS_TO_INDEX):
			break		
		
	return kmer_idx_pipe

def index_read(params):
	(	reads_data,
		reads_offset,
		barcodes_data, 
		barcodes_offset) = params
		
	kmer_index = {}
	read_kmers = IO_utils.get_cyclic_kmers(
		barcodes_data, 
		args['kmer_size'],
		args['barcode_start'], 
		args['barcode_end'])
	for(kmer, _) in read_kmers:
		if(kmer not in kmer_index.keys()):
			kmer_index[kmer] = []
		kmer_index[kmer].append(barcodes_offset)
	return kmer_index

def get_kmer_counts(kmer_idx_db, kmer_idx_pipe):
	kmer_counts = {}
	for key in kmer_idx_db.keys():
		entries = IO_utils.get_from_db(kmer_idx_pipe,[key])[0]
		kmer_counts[key.decode('utf-8')] = len(entries)
	return kmer_counts

def find_paths(params):
	(	kmer_idx_pipe,
		kmer_counts,
		barcodes_unzipped, 
		reads_unzipped) = params
	barcode_length = args['barcode_end'] - args['barcode_start']
	kmers_sorted = [tup[0] for tup in sorted(
		list(kmer_counts.items()),
		key = lambda tup: tup[1],
		reverse = True)]
	
	starting_kmers = []
	for kmer in kmers_sorted:
		if(kmer[0] == '$'):
			starting_kmers.append(kmer)
		if(len(starting_kmers) >= args['breadth']):
			break	
	
	pool = Pool(processes = args['threads'])
	paths = []
	for kmers_group in IO_utils.grouper(starting_kmers, args['threads']):
		offsets_group = IO_utils.get_from_db(kmer_idx_pipe, kmers_group)
		paths_group = pool.map(find_path_from_kmer, zip(
				kmers_group,
				offsets_group,
				itertools.repeat(barcodes_unzipped),
				itertools.repeat(barcode_length)))
		paths += [item for sublist in paths_group for item in sublist]
	return paths

def find_path_from_kmer(params):
	(	starting_kmer,
		offsets, 
		barcodes_unzipped,
		barcode_length) = params
	#1. build subgraph
	subgraph = build_subgraph(offsets, barcodes_unzipped)
	#2. find paths
	node = starting_kmer[0:-1]
	neighbor = starting_kmer[1:]
	paths = []
	paths_iter = subgraph.find_all_cyclic_paths(
			node, neighbor, barcode_length + 1)
	counter = 1
	while(True):
		try:
			path = next(paths_iter)
		except StopIteration:
			break
		if(not path.is_cycle()):
			break
		
		seq = path.get_sequence_circular()
		weight = path.get_cycle_weight()
		nodes = [edge.get_sequence() for edge in path.edges]
		paths.append( (seq, weight, counter, nodes) )
		if(counter > args['depth']):
			break
		counter += 1
	return paths

def build_subgraph(reads_in_subgraph, barcodes_unzipped):
	barcodes_iter = IO_utils.read_fastq_random(
		barcodes_unzipped, reads_in_subgraph)
	subgraph_kmer_counts = collections.Counter()
	while(True):
		try:
			barcode_data, _ = next(barcodes_iter)
		except StopIteration:
			break	
		read_kmers = IO_utils.get_cyclic_kmers(
			barcode_data, 
			int(args['kmer_size']),
			int(args['barcode_start']), 
			int(args['barcode_end']))		
		for (kmer, _ ) in read_kmers:
			subgraph_kmer_counts[kmer] += 1
	edges = []
	for(kmer, count) in subgraph_kmer_counts.items():
		edge = Edge(kmer[0:-1], kmer[1:], count)
		edges.append(edge)
	subgraph = Graph(edges)	
	return subgraph

def merge_paths(paths):
	MIN_DIST = 3
	paths_sorted = sorted(paths, key = lambda tup: tup[1])
	num_paths = len(paths)
	
	get_seq = lambda paths, i: paths[i][0]
	paths_merged = {tup[0] : tup for tup in paths_sorted}
	
	for (i, path) in enumerate(paths_sorted):
		for j in range(i+1, num_paths):
			hamming = hamming_distance(get_seq(paths, i), get_seq(paths, j))
			if(hamming <= MIN_DIST):
				bad_path = min([paths[i], paths[j]], key = lambda tup: tup[1])
				if(bad_path[0] in paths_merged.keys()):
					del(paths_merged[bad_path[0]])
	return list(paths_merged.values())

def hamming_distance(seq1, seq2):
	hamming = 0
	for (i,j) in zip(seq1, seq2):
		if(i != j):
			hamming += 1
	return hamming

def threshold_paths(output_dir, paths):
	import matplotlib as mpl
	mpl.use('Agg')
	from matplotlib import pyplot as plt
	from scipy.optimize import curve_fit
	
	
	fit_out = {
		'gaussian_fits' : '%s/fits.txt' % output_dir,
		'paths_plot' : '%s/paths_plotted.pdf' % output_dir}
	
	weights_by_depth = {}
	for path in paths:
		(name, weight, depth, kmers) = path
		if(depth not in weights_by_depth.keys()):
			weights_by_depth[depth] = []
		weights_by_depth[depth].append(int(weight))
	
	fig, ax = plt.subplots(
		nrows = len(weights_by_depth.items()), ncols = 1, figsize = (4,8))
	colors = ['b', 'g', 'r', 'y', 'k']
	
	gaussian_fits = []
	
	for (i, key) in enumerate(sorted(weights_by_depth.keys())):
		weights = weights_by_depth[key]
		bins = np.logspace(0, 8, 50)
		hist, bins = np.histogram(weights, bins=bins)
		bin_centers = (bins[:-1] + bins[1:])/2

		p0 = [100, 25, 10]
		fit_x = range(0, len(hist))#xrange corresponding to bins only
		coeff, var_matrix = curve_fit(gaussian, fit_x, hist, p0=p0)
		hist_fit = gaussian(fit_x, *coeff)
		(amplitude, mean, stdev)= coeff
		threshold_bin = int(mean + 3*np.fabs(stdev))
		threshold = bins[min(threshold_bin, len(bins) - 1)]
		gaussian_fits.append((i+1, amplitude, mean, stdev, threshold))
		
		ax[i].step(bin_centers, hist, label = 'Depth = %i' % key, color = colors[i])		
		ax[i].axvline(threshold, color = 'grey', ls = '-.', label='Mean + 3 stdev')
		ax[i].plot(bin_centers, hist_fit, label='Gaussian fit', color = 'grey')

		ax[i].set_xscale('log')
		ax[i].legend(loc='best', fontsize=8)
		ax[i].set_xlabel('Path capacity')
		ax[i].set_ylabel('Count')
	plt.tight_layout()
	
	fig.savefig(fit_out['paths_plot'])
	with open(fit_out['gaussian_fits'], 'w') as writer:
		writer.write(
			'\t'.join(['depth', 'amplitude', 'mean', 'stdev', 'threshold']) + '\n')
		for tup in gaussian_fits:
			line = '\t'.join([str(i) for i in tup]) + '\n'
			writer.write(line)
	
	threshold = gaussian_fits[1][4]
	top_paths = [path for path in paths if path[1] > threshold]
	return threshold, top_paths, fit_out

def gaussian(x, *p):
	a, mu, sigma = p
	return a*np.exp(-(x-mu)**2/(2.*sigma**2))



"""
def plot_paths(output_dir, hist, bins, threshold, prefix=''):
	import matplotlib as mpl
	mpl.use('Agg')
	from matplotlib import pyplot as plt
	
	plot_file = '%s/%s_paths_plot.pdf' % (output_dir, prefix)
	fig, ax = plt.subplots(nrows = 1, ncols = 1, figsize = (4,4))
	ax.step(bins[0:-1], hist)
	ax.set_xscale('log')
	#ax.set_yscale('log')
	ax.set_xlabel('Path capacity')
	ax.set_ylabel('Count')
	ax.set_xlim([10**1, 10**7])
	ax.axvline(threshold, ls='--', color = 'k')
	plt.tight_layout()
	fig.savefig(plot_file)
	
	return(plot_file)
	
def get_histogram_thresholded(paths):
	import numpy as np
	import scipy as sp
	from scipy import signal
	
	bins = np.logspace(0, 8, 50)
	path_weight = [tup[1] for tup in paths]
	hist, bins = np.histogram(path_weight, bins=bins)
	#find all local minimum in histogram
	minima = sp.signal.argrelmin(hist, order=1)
	#take the right most local minimum
	try:
		bin_to_threshold = (minima[0][0])#???
		weight_threshold = bins[bin_to_threshold]
	except IndexError:
		weight_threshold = 0
	
	top_paths = []
	for tup in paths:
		if(tup[1] >= weight_threshold):
			top_paths.append(tup)
	return(top_paths, hist, bins, weight_threshold)
"""

def assign_all_reads(top_paths, reads_unzipped, barcodes_unzipped):
	#initialize vars
	reads_assigned_db, reads_assigned_pipe = IO_utils.initialize_redis_pipeline(db=1)
	kmers_to_paths = {}
	
	print('\tGetting kmers in paths')
	for path in top_paths:
		cell_barcode = path[0]
		kmers = path[3]
		for kmer in kmers:
			if(kmer not in kmers_to_paths.keys()):
				kmers_to_paths[kmer] = []
			kmers_to_paths[kmer].append(cell_barcode)
		#####
		#also do this for k-1 mers etc
		#####
	print('\tAssigning reads to paths')
	pool = Pool(processes = args['threads'])	
	read_count = 0
	for reads_chunk in IO_utils.get_read_chunks(
		args,
		barcodes_unzipped,
		reads_unzipped,
		lines=None):
		
		read_count += len(reads_chunk)
		assignments = pool.map(assign_read, 
			zip(itertools.repeat(kmers_to_paths), reads_chunk))
		for (assignment, offset1, offset2) in assignments:
			reads_assigned_pipe.append(
				assignment.encode('utf-8'), 
				('%i,%i,' % (offset1, offset2)).encode('utf-8'))
		reads_assigned_pipe.execute()
		print('\t%i reads assigned' % read_count)
		
	return(reads_assigned_db, reads_assigned_pipe)
	
def assign_read(params):
	(kmers_to_paths,
		(reads_data,
		reads_offset,
		barcodes_data, 
		barcodes_offset)) = params
	read_kmers = IO_utils.get_cyclic_kmers(
		barcodes_data, 
		args['kmer_size'],
		args['barcode_start'], 
		args['barcode_end'])
	read_assignment = collections.Counter()
	
	for (kmer, _ ) in read_kmers:
		paths_with_kmer = kmers_to_paths.get(kmer, [])
		for path in paths_with_kmer:
			read_assignment[path] += 1
	most_common = read_assignment.most_common(1)
	assignment = 'unassigned'
	if(len(most_common) == 1):
		assignment = most_common[0][0]
	return (assignment, reads_offset, barcodes_offset)

def write_split_fastqs(params):
	import gzip
	(	reads_assigned_db,
		reads_assigned_pipe,
		output_dir,
		reads_unzipped,
		barcodes_unzipped) = params
	
	split_dir = '%s/reads_split' % output_dir
	if not os.path.exists(split_dir):
		os.makedirs(split_dir)
	output_files = {'batch' : '%s/batch.txt' % (split_dir)}
	batch_file = open(output_files['batch'], 'w')
		
	for cell in reads_assigned_db.keys():
		cell_name = 'cell_%s' % cell.decode('utf-8')
		print('\tWorking on cell %s' % cell_name)
		
		output_files[cell_name] = {
			'reads' : '%s/%s_reads.fastq.gz' % (split_dir, cell_name),
			'barcodes' : '%s/%s_barcodes.fastq.gz' % (split_dir, cell_name),
			'umi' : '%s/%s.umi.txt' % (split_dir, cell_name)}
		batch_file.write('%s\t%s\t%s\n' % \
			(cell_name, 
			output_files[cell_name]['umi'], 
			output_files[cell_name]['reads']))
		reads_writer = gzip.open(output_files[cell_name]['reads'], 'wb')
		barcodes_writer = gzip.open(output_files[cell_name]['barcodes'], 'wb')
		umi_writer = open(output_files[cell_name]['umi'], 'wb')
		
		cell_offsets = IO_utils.get_from_db(reads_assigned_pipe, [cell])[0]
		assert len(cell_offsets) % 2 == 0, \
			'Cell offsets must contain an even number of entries'
		reads_iter = IO_utils.read_fastq_random(
			reads_unzipped, 
			[cell_offsets[i] for i in range(len(cell_offsets)) if i % 2 == 0])
		barcodes_iter = IO_utils.read_fastq_random(
			barcodes_unzipped,
			[cell_offsets[i] for i in range(len(cell_offsets)) if i % 2 == 1])
		
		while(True):
			try:
				reads_data, _ = next(reads_iter)
				barcodes_data, _ =  next(barcodes_iter)
			except StopIteration:
				break
			reads_data[0] += ' %s' % cell_name.replace('_', ':')
			reads_data[0] = reads_data[0].replace(' ', '_')
			barcodes_data[0] += ' %s' % cell_name.replace('_', ':')	
			barcodes_data[0] = barcodes_data[0].replace(' ', '_')
					
			umi = barcodes_data[1][
				int(args['umi_start']): int(args['umi_end'])]
			reads_writer.write(
				('\n'.join(reads_data) + '\n').encode('utf-8'))
			barcodes_writer.write(
				('\n'.join(barcodes_data) + '\n').encode('utf-8'))
			umi_writer.write((umi + '\n').encode('utf-8'))
		
		reads_writer.close()
		umi_writer.close()
		barcodes_writer.close()
	batch_file.close()
	return output_files








def get_args():
	parser = argparse.ArgumentParser(
		description = 'This script splits reads for dropseq data')
	parser.add_argument('--barcodes', 
		type=str, 
		help='Barcodes file name (unzipped)', 
		required=True)
	parser.add_argument('--reads', 
		type=str, 
		help='RNAseq reads file name (unzipped)', 
		required=True)
	parser.add_argument('--output_dir', 
		type=str, 
		help='Directory where outputs are written', 
		required=True)
	parser.add_argument('--barcode_start', 
		type=int, 
		help='Start position of barcode.', 
		default=0)
	parser.add_argument('--barcode_end', 
		type=int, 
		help='End position of barcode.', 
		default=12)
	parser.add_argument('--umi_start', 
		type=int, 
		help='Start position of UMI.', 
		default=12)
	parser.add_argument('--umi_end', 
		type=int, 
		help='End position of UMI.', 
		default=20)
	parser.add_argument('--kmer_size', 
		type=int, 
		help='Size of kmers for making barcode De Bruijn graph.', 
		default=7)
	parser.add_argument('--depth', 
		type=int, 
		help='Fraction of edge weight at starting node to assign to path.', 
		default=3)
	parser.add_argument('--breadth', 
		type=int, 
		help='How many nodes search.', 
		default=1000)
	parser.add_argument('--threads', 
		type=int, 
		help='Number of threads to use.', 
		default=32)
	
	return vars(parser.parse_args())

if __name__ == '__main__':
	cmdline_args = get_args()	
	output_files, elapsed_time = run_all(cmdline_args)
	print('Done. Time elapsed: %f seconds' % elapsed_time)



