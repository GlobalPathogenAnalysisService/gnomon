'''gnomonicus.py is a library providing functions which pull together output VCF of the Lodestone TB pipeline
    with a reference genome and a resistance catalogue, and utilise gumpy and
    piezo to produce variants, mutations and an antibiogram.

Based on sp3predict
'''
import copy
import datetime
import gzip
import json
import logging
import os
import pickle
import re
import traceback
import warnings
from collections import defaultdict
from collections.abc import Iterable

import gumpy
import numpy as np
import pandas as pd
import piezo
from tqdm import tqdm


class InvalidMutationException(Exception):
    '''Custom exception raised when an invalid mutation is detected
    '''
    def __init__(self, gene: str, mutation: str):
        '''Raise this exception

        Args:
            gene (str): Name of the gene
            mutation (str): The invalid mutation
        '''
        self.message = f"{gene}@{mutation} is not a valid mutation!"
        super().__init__(self.message)

class OutdatedGumpyException(Exception):
    '''Custom exception raised if a pickled gumpy.Genome object doesn't match
    the version currently being used here. 
    '''
    def __init__(self):
        self.message = "This Genome object is outdated! Pass a genbank file to re-instanciate"
        super().__init__(self.message)

def checkGzip(path: str) -> bool:
    '''Check if a given path is a gzipped file

    Args:
        path (str): Path to the file

    Returns:
        bool: True if the file is gzipped
    '''
    try:
        with gzip.open(path) as f:
            f.read()
        return True
    except:
        return False


def loadGenome(path: str, progress: bool) -> gumpy.Genome:
    '''Load a genome from a given path. Checks if path is to a pickle dump, or if a pickle dump of the path's file exists
    Instanciates a new gumpy Genome and dumps to pickle as a last resort

    Args:
        path (str): Path to the genbank file or pickle dump. If previously run, a genbank file's Genome object is pickled and dumped for speed
        progress (bool): Boolean as whether to show progress bar for gumpy

    Returns:
        gumpy.Genome: Genome object of the reference genome
    '''
    logging.debug(f"Using file {path}")
    #Remove trailing '/' if required
    if path[-1] == '/':
        path = path[:-1]

    #Check if the file is gzipped
    gzipped = checkGzip(path)

    #Get the gumpy version we are using
    gumpy_major, gumpy_minor, gumpy_maintainance = gumpy.__version__[1:].split(".")
    outdated = False

    #Try to load as a pickle
    try:
        if gzipped:
            logging.info("Path was to a gzipped file. Decompressing...")
            f = gzip.open(path, 'rb')
        else:
            logging.info("Path was not to a gzipped file. Defaulting to normal reading")
            f = open(path, 'rb')
        g = pickle.load(f)
        if hasattr(g, 'gumpy_version'):
            #Has the version set, so check it
            major, minor, maintainance = g.gumpy_version[1:].split(".")
            if major == gumpy_major and minor == gumpy_minor and maintainance == gumpy_maintainance:
                #Exact match to the gumpy version
                return g
            else:
                outdated = True
        else:
            outdated = True
        if outdated:
            logging.error("Genome object is outdated!")
            raise OutdatedGumpyException()
    except OutdatedGumpyException as e:
        logging.error("Genome object is outdated!")
        raise e
    except Exception as e:
        logging.info(f"Genome object not a pickle, checking if pickled version exists. Error: {e}")

    #Try pickled version created by this (path+'.pkl')
    #Check if this file is gzipped
    gzipped = checkGzip(path+".pkl")
    try:
        if gzipped:
            logging.info("Path was to a gzipped file. Decompressing...")
            f = gzip.open(path+".pkl", 'rb')
        else:
            logging.info("Path was not to a gzipped file. Defaulting to normal reading")
            f = open(path+".pkl", 'rb')
        g = pickle.load(f)
        if hasattr(g, 'gumpy_version'):
            #Has the version set, so check it
            major, minor, maintainance = g.gumpy_version[1:].split(".")
            if major == gumpy_major and minor == gumpy_minor and maintainance == gumpy_maintainance:
                #Exact match to the gumpy version
                return g
            else:
                outdated = True
        else:
            outdated = True
        if outdated:
            logging.info("Genome object is outdated! Trying with the original filepath")
    except Exception as e:
        logging.info(f"No pickled version of genome object, instanciating and dumping. Error: {e}")
    
    #Create new gumpy.Genome and pickle dump for speed later
    reference = gumpy.Genome(path, show_progress_bar=progress)
    reference.gumpy_version = gumpy.__version__
    pickle.dump(reference, open(path+'.pkl', 'wb'))
    return reference

def populateVariants(vcfStem: str, outputDir: str, diff: gumpy.GenomeDifference, make_csv: bool, resistanceGenesOnly: bool, catalogue: piezo.ResistanceCatalogue=None) -> pd.DataFrame:
    '''Populate and save the variants DataFrame as a CSV

    Args:
        vcfStem (str): The stem of the filename for the VCF file. Used as a uniqueID
        outputDir (str): Path to the desired output directory
        diff (gumpy.GenomeDifference): GenomeDifference object between reference and the sample
        make_csv (bool): Whether to write the CSV of the dataframe
        catalogue (piezo.ResistanceCatalogue, optional): Catalogue for determining FRS or COV for minority populations. If None is given, FRS is assumed. Defaults to None
    
    Returns:
        pd.DataFrame: DataFrame of the variants
    '''
    #Populate variants table directly from GenomeDifference
    vals = {
            'variant': diff.variants, 
            'nucleotide_index': diff.nucleotide_index,
            'indel_length': diff.indel_length,
            'indel_nucleotides': diff.indel_nucleotides,
            'vcf_evidence': [json.dumps(x) for x in diff.vcf_evidences],
            'vcf_idx': diff.vcf_idx,
            'gene': diff.gene_name,
            'gene_position': diff.gene_pos,
            'codon_idx': diff.codon_idx
            }

    #Use of Int64 rather than int is required here as pandas doesn't allow mixed int/None
    variants = pd.DataFrame(vals).astype(
        {
            'vcf_evidence': 'object',
            'nucleotide_index': 'Int64',
            'indel_length': 'Int64',
            'vcf_idx': 'Int64',
            'gene_position': 'Int64',
            'codon_idx': 'Int64'
        }
    )

    if catalogue is not None:
        #Figure out if we want to keep all of the variants
        genes = getGenes(diff, catalogue, resistanceGenesOnly)
        to_drop = []
        for idx, row in variants.iterrows():
            if row['gene'] not in genes:
                #Not a variant we're interested in, so remove
                to_drop.append(idx)
                
        variants.drop(index=to_drop, inplace=True)
    else:
        genes = set(diff.genome2.genes.keys())

    if diff.genome1.minor_populations or diff.genome2.minor_populations:
        variants = pd.concat([variants, minority_population_variants(diff, catalogue, genes)])

    #If there are variants, save them to a csv
    if not variants.empty:
        #Add unique ID to each record
        variants['uniqueid'] = vcfStem

        variants = variants[['uniqueid', 'variant', 'gene', 'gene_position', 'codon_idx', 'nucleotide_index', 'indel_length', 'indel_nucleotides', 'vcf_evidence', 'vcf_idx']]
        variants = variants.drop_duplicates()
        if make_csv:
            #Save CSV
            variants.to_csv(os.path.join(outputDir, f'{vcfStem}.variants.csv'), header=True, index=False)
    variants.reset_index(inplace=True)
    return variants

def get_minority_population_type(catalogue: piezo.ResistanceCatalogue) -> str:
    '''Figure out if a catalogue uses FRS or COV. If neither or both, default to FRS

    Args:
        catalogue (piezo.ResistanceCatalogue): Catalogue

    Returns:
        str: Either 'percentage' or 'reads' for FRS or COV respectively
    '''
    if catalogue is None:
        #Nothing given, so default to FRS
        return 'percentage'
    frs = 0
    cov = 0
    for minor in catalogue.catalogue.rules['MINOR']:
        for m in minor.split(","):
            if m:
                m = float(m)
                assert m > 0, f"Minor populations must be positive: {m}"
                if m < 1:
                    #FRS
                    frs += 1
                else:
                    #COV
                    cov += 1
    #We have just COV
    if cov > 0 and frs == 0:
        return 'reads'
    #We have anything else
    return 'percentage'

def getGenes(diff: gumpy.GenomeDifference, resistanceCatalogue: piezo.ResistanceCatalogue, resistanceGenesOnly: bool) -> set:
    '''Get the genes we're interested in. 
    
    This is either just resistance genes which have variants, or all which have variants

    Args:
        diff (gumpy.GenomeDifference): Genome Difference between reference and sample
        resistanceCatalogue (piezo.ResistanceCatalogue): Resistance catalogue
        resistanceGenesOnly (bool): Whether to just use genes within the catalogue

    Returns:
        set[str]: Set of gene names
    '''
    reference = diff.genome1
    sample = diff.genome2
    if resistanceCatalogue:
        if resistanceGenesOnly:
            resistanceGenes = set(resistanceCatalogue.catalogue.genes)
        else:
            resistanceGenes = set(sample.genes)
        #Find the genes which have mutations regardless of being in the catalogue
        #Still cuts back time considerably, and ensures all mutations are included in outputs
        mask = np.isin(reference.stacked_nucleotide_index, diff.nucleotide_index)
        genesWithMutations = np.unique(reference.stacked_gene_name[mask]).tolist()

        #Make sure minority population mutations are also picked up
        minor_genes = set()
        for population in sample.minor_populations:
            for gene in reference.stacked_gene_name[reference.stacked_nucleotide_index == population[0]]:
                if gene:
                    minor_genes.add(gene)
        genesWithMutations += minor_genes

        deletions = []
        #Make sure large deletions are picked up too
        for name in reference.stacked_gene_name:
            deletions += np.unique(name[sample.is_deleted]).tolist()
        genesWithMutations = set(genesWithMutations + deletions)

        return genesWithMutations.intersection(resistanceGenes)

    else:
        #No catalogue, so just stick to genes in the sample
        return sample.genes

def populateMutations(
        vcfStem: str, outputDir: str, diff: gumpy.GenomeDifference, reference: gumpy.Genome,
        sample: gumpy.Genome, resistanceCatalogue: piezo.ResistanceCatalogue, make_csv: bool, resistanceGenesOnly: bool) -> (pd.DataFrame, dict, dict):
    '''Popuate and save the mutations DataFrame as a CSV, then return it for use in predictions

    Args:
        vcfStem (str): The stem of the filename of the VCF file. Used as a uniqueID
        outputDir (str): Path to the desired output directory
        diff (gumpy.GenomeDifference): GenomeDifference object between reference and this sample
        reference (gumpy.Genome): Reference genome
        sample (gumpy.Genome): Sample genome
        resistanceCatalogue (piezo.ResistanceCatalogue): Resistance catalogue (used to find which genes to check)
        make_csv (bool): Whether to write the CSV of the dataframe
        resistanceGenesOnly (bool): Whether to use just genes present in the resistance catalogue

    Raises:
        MissingFieldException: Raised when the mutations DataFrame does not contain the required fields

    Returns:
        pd.DataFrame: The mutations DataFrame
        dict: Dictionary mapping gene name --> reference gumpy.Gene object
    '''
    genesWithMutations = getGenes(diff, resistanceCatalogue, resistanceGenesOnly)

    #Iter resistance genes with variation to produce gene level mutations - concating into a single dataframe
    mutations = None
    referenceGenes = {}
    diffs = []
    #This is where the majority of the time to process is used.
    #However, I have tried a few ways to improve this involving reducing reliance on DF concat
    #This has potential for improvement (an actual TB sample can take ~2.5mins), but likely will
    #   come from optimising the underlying gumpy.GeneDifference code...
    for gene in tqdm(genesWithMutations):
        if gene:
            logging.debug(f"Found a gene with mutation: {gene}")
            #Save the reference genes for use later in effects.csv
            refGene = reference.build_gene(gene)
            referenceGenes[gene] = refGene
            #Get gene difference
            diff = refGene - sample.build_gene(gene)
            diffs.append(diff)

            #Pull the data out of the gumpy object
            vals = {
                'mutation': diff.mutations,
                'nucleotide_number': diff.nucleotide_number,
                'nucleotide_index': diff.nucleotide_index,
                'gene_position': diff.gene_position,
                'alt': diff.alt_nucleotides,
                'ref': diff.ref_nucleotides,
                'codes_protein': [diff.codes_protein and pos > 0 if pos is not None else diff.codes_protein for pos in diff.gene_position],
                'indel_length': diff.indel_length,
                'indel_nucleotides': diff.indel_nucleotides,
                }
            #As diff does not populate amino acid items for non-coding genes,
            #pull out the sequence or default to None
            if refGene.codes_protein:
                vals['amino_acid_number'] = diff.amino_acid_number
                aa_seq = []
                #Pull out the amino acid sequence from the alt codons
                for idx, num in enumerate(diff.amino_acid_number):
                    if num is not None:
                        aa_seq.append(refGene.codon_to_amino_acid[diff.alt_nucleotides[idx]])
                    else:
                        aa_seq.append(None)
                vals['amino_acid_sequence'] = np.array(aa_seq)
            else:
                vals['amino_acid_number'] = None
                vals['amino_acid_sequence'] = None
            
            vals['number_nucleotide_changes'] = [sum(i!=j for (i,j) in zip(r, a)) if r is not None and a is not None else None for r, a in zip(vals['ref'], vals['alt'])]
            
            geneMutations = pd.DataFrame(vals)
            #Add gene name
            geneMutations['gene'] = gene

            #Add this gene's mutations to the total dataframe
            if not geneMutations.empty:
                if mutations is not None:
                    mutations = pd.concat([mutations, geneMutations])
                else:
                    mutations = geneMutations
    if mutations is not None:
        #Ensure correct datatypes
        mutations = mutations.astype({'mutation': 'str',
                                    'gene': 'str',
                                    'nucleotide_number': 'Int64',
                                    'nucleotide_index': 'Int64',
                                    'gene_position': 'Int64',
                                    'alt': 'str',
                                    'ref': 'str',
                                    'codes_protein': 'bool',
                                    'indel_length': 'Int64',
                                    'indel_nucleotides': 'str',
                                    'amino_acid_number': 'Int64',
                                    'amino_acid_sequence': 'str',
                                })
    #Add minor mutations (these are stored separately)
    if reference.minor_populations or sample.minor_populations:
        #Only do this if they exist
        x, errors = minority_population_mutations(diffs, resistanceCatalogue)
        mutations = pd.concat([mutations, x])
    else:
        errors = {}
    #If there were mutations, write them to a CSV
    if mutations is not None:

        #Add the number of mutations which occured for this mutation

        #Add VCF stem as the uniqueID
        mutations['uniqueid'] = vcfStem

        if make_csv:
            #Reorder the columns
            mutations = mutations[['uniqueid', 'gene', 'mutation', 'ref', 'alt', 'nucleotide_number', 'nucleotide_index', 'gene_position', 'codes_protein', 'indel_length', 'indel_nucleotides', 'amino_acid_number', 'amino_acid_sequence', 'number_nucleotide_changes']]
            
            #As we have concated several dataframes, the index is 0,1,2,0,1...
            #Reset it so that we can use it to delete
            mutations.reset_index(drop=True, inplace=True)
            #Filter out nucleotide variants from synonymous mutations to avoid duplication of data
            mutations_ = copy.deepcopy(mutations)
            to_drop = []
            for idx, row in mutations_.iterrows():
                if row['codes_protein'] and row['ref'] is not None and row['alt'] is not None:
                    #Protein coding so check if nucleotide within coding region
                    if len(row['ref']) == 1:
                        #Nucleotide SNP
                        to_drop.append(idx)
            mutations_.drop(index=to_drop, inplace=True)
            #Save it as CSV
            mutations_.to_csv(os.path.join(outputDir, f'{vcfStem}.mutations.csv'), index=False)

    return mutations, referenceGenes, errors

def minority_population_variants(diff: gumpy.GenomeDifference, catalogue: piezo.ResistanceCatalogue, genes: set) -> pd.DataFrame:
    '''Handle the logic for pulling out minority population calls for genome level variants

    Args:
        diff: (gumpy.GenomeDifference): GenomeDifference object for this comparison
        catalogue: (piezo.ResistanceCatalogue): Catalogue to use. Used for determining whether FRS or COV should be used
        genes (set[str]): Set of gene names we care about

    Returns:
        pd.DataFrame: DataFrame containing the minority population data
    '''
    #Determine if FRS or COV should be used
    minor_type = get_minority_population_type(catalogue)

    #Get the variants in GARC
    variants_ = diff.minor_populations(interpretation=minor_type)

    nucleotide_indices = []
    indel_lengths = []
    indel_nucleotides = []
    vcf_evidences = []
    vcf_idx = []
    gene_name = []
    gene_pos = []
    codon_idx = []
    variants = []

    if genes is not None:
        #Using the gene names, find out which genome indices we want to look out for
        indices_we_care_about = []
        for gene in genes:
            mask = diff.genome1.stacked_gene_name == gene
            gene_indices = diff.genome1.stacked_nucleotide_index[mask].tolist()
            indices_we_care_about += gene_indices
        indices_we_care_about = set(indices_we_care_about)
    else:
        #Genes was none, so fetch everything
        indices_we_care_about = set(diff.genome1.nucleotide_index)


    for variant_ in variants_:
        variant, evidence = variant_.split(":")
        variants.append(variant_)
        
        if ">" in variant:
            idx = int(variant.split(">")[0][:-1])
            if idx not in indices_we_care_about:
                variants.pop()
                continue
            nucleotide_indices.append(idx)
            vcf = diff.genome2.vcf_evidence.get(int(variant.split(">")[0][:-1]))
            vcf_evidences.append(json.dumps(vcf))
            
            ref = variant.split(">")[0][-1]
            alt = variant.split(">")[-1]

            if ref == alt:
                #Wildtype call so return 0 as it isn't an ALT
                vcf_idx.append(0)
            else:
                #Simple SNP so check for presence in alts + right COV
                ev = float(evidence)
                if ev < 1:
                    #We have FRS so (due to rounding) convert the VCF's COV to FRS
                    total_depth = sum(vcf['COV'])
                    cov = [round(x/total_depth, 3) for x in vcf["COV"]]
                else:
                    cov = vcf['COV']
                minor_call = variant.split(">")[-1]

                added = False
                for v_idx, alt in enumerate(vcf['ALTS']):
                    v_idx += 1
                    if added:
                        #Already added so break
                        break
                    for i, a in enumerate(alt):
                        if a == minor_call and idx == vcf["POS"] + i:
                            #Match on call and position
                            #Double check that the COV matches too
                            if ev == cov[v_idx]:
                                #This is the right element
                                vcf_idx.append(v_idx)
                                added = True

                #Shouldn't be possible, but check anyway
                if added is False:
                    warnings.warn(
                        f"The index of the VCF evidence could not be determined! {variant_} --> {vcf}. "
                        "Continuing with None values")
                    vcf_idx.append(None)



        else:
            idx = int(variant.split("_")[0])
            if idx not in indices_we_care_about:
                variants.pop()
                continue
            nucleotide_indices.append(idx)
            vcf = diff.genome2.vcf_evidence.get(int(variant.split("_")[0]))
            vcf_evidences.append(json.dumps(vcf))

            #Match VCF idx on base changes + cov
            ev = float(evidence)
            if ev < 1:
                #We have FRS so (due to rounding) convert the VCF's COV to FRS
                total_depth = sum(vcf['COV'])
                cov = [round(x/total_depth, 3) for x in vcf["COV"]]
            else:
                cov = vcf['COV']
            
            ref = vcf['REF']
            type_ = variant.split("_")[1]
            bases = variant.split("_")[-1]
            added = False
            for v_idx, alt in enumerate(vcf['ALTS']):
                if added:
                    break
                v_idx += 1
                #Use the same `simplify_call` method to decompose the ALTs into indels + snps
                #Match on the indel + pos + cov
                for call in diff.genome2.vcf_file._simplify_call(ref, alt):
                    offset, t, b = call
                    if vcf['POS'] + offset == idx and type_ == t and bases == b:
                        #Match on pos, type and bases so check cov too
                        if ev == cov[v_idx]:
                            added = True
                            vcf_idx.append(v_idx)

            if added is False:
                warnings.warn(
                    f"The index of the VCF evidence could not be determined! {variant_} --> {vcf}. "
                    "Continuing with None values")
                vcf_idx.append(None)                      
            
        if "_" in variant:
            indel_lengths.append(len(variant.split("_")[-1]))
            indel_nucleotides.append(variant.split("_")[-1])
        else:
            indel_lengths.append(0)
            indel_nucleotides.append(None)

        #Find the genes at this pos
        genes = sorted(list(set(diff.genome1.stacked_gene_name[diff.genome1.stacked_nucleotide_index == idx])))
        if len(genes) > 1:
            #If we have genes, we need to duplicate some info
            first = True
            for gene in genes:
                if gene == '':
                    #If we have genes, we don't care about this one
                    continue
                    
                gene_name.append(gene)
                gene_pos.append(diff.get_gene_pos(gene, idx, variant))
                if diff.genome1.genes[gene]['codes_protein'] and gene_pos[-1] > 0:
                    #Get codon idx
                    nc_idx = diff.genome1.stacked_nucleotide_index[diff.genome1.stacked_gene_name == gene]
                    nc_num = diff.genome1.stacked_nucleotide_number[diff.genome1.stacked_gene_name == gene]
                    codon_idx.append(nc_num[nc_idx == idx][0] % 3)
                else:
                    codon_idx.append(None)
                
                #If this isn't the first one, we need to duplicate the row
                if first:
                    first = False
                else:
                    variants.append(variants[-1])
                    nucleotide_indices.append(nucleotide_indices[-1])
                    indel_lengths.append(indel_lengths[-1])
                    indel_nucleotides.append(indel_nucleotides[-1])
                    vcf_evidences.append(vcf_evidences[-1])
                    vcf_idx.append(vcf_idx[-1])

        else:
            #We have 1 gene or none, so set to None if no gene is present
            gene = genes[0] if genes[0] != '' else None
            if gene is not None:
                #Single gene, so pull out data
                gene_name.append(gene)
                gene_pos.append(diff.get_gene_pos(gene, idx, variant))

                if diff.genome1.genes[gene]['codes_protein'] and gene_pos[-1] > 0:
                    #Get codon idx
                    nc_idx = diff.genome1.stacked_nucleotide_index[diff.genome1.stacked_gene_name == gene]
                    nc_num = diff.genome1.stacked_nucleotide_number[diff.genome1.stacked_gene_name == gene]
                    codon_idx.append(nc_num[nc_idx == idx][0] % 3)
                else:
                    codon_idx.append(None)
            else:
                gene_name.append(None)
                gene_pos.append(None)
                codon_idx.append(None)



    vals = {
        'variant': variants, 
        'nucleotide_index': nucleotide_indices,
        'indel_length': indel_lengths,
        'indel_nucleotides': indel_nucleotides,
        'vcf_evidence': vcf_evidences,
        'vcf_idx': vcf_idx,
        'gene': gene_name,
        'gene_position': gene_pos,
        'codon_idx': codon_idx
        }
    #Convert everything to numpy arrays
    vals = {key: np.array(vals[key]) for key in vals.keys()}
    return pd.DataFrame(vals).astype({
                                    'vcf_evidence': 'object',
                                    'nucleotide_index': 'Int64',
                                    'indel_length': 'Int64',
                                    'vcf_idx': 'Int64',
                                    'gene_position': 'Int64',
                                    'codon_idx': 'Int64'
                                })


def minority_population_mutations(diffs: [gumpy.GeneDifference], catalogue: piezo.ResistanceCatalogue) -> (pd.DataFrame, dict):
    '''Handle the logic for pulling out minority population calls for gene level variants

    Args:
        diffs ([gumpy.GeneDifference]): List of GeneDifference objects for these comparisons
        catalogue: (piezo.ResistanceCatalogue): Catalogue to use. Used for determining whether FRS or COV should be used

    Returns:
        pd.DataFrame: DataFrame containing the minority population data
    '''
    #Get the mutations
    mutations_ = []
    genes = []
    gene_pos = []
    nucleotide_number = []
    nucleotide_index = []
    alt = []
    ref = []
    codes_protein = []
    indel_length = []
    indel_nucleotides = []
    is_cds = []
    is_het = []
    is_null = []
    is_promoter = []
    is_snp = []
    aa_num = []
    aa_seq = []
    variants = []
    number_nucleotide_changes = []

    #Track errors (if any) for reporting but not throwing
    errors = {}

    #Determine if FRS or COV should be used
    minor_type = get_minority_population_type(catalogue)

    for diff in diffs:
        #As mutations returns in GARC, split into constituents for symmetry with others
        try:
            mutations = diff.minor_populations(interpretation=minor_type)
        except Exception as e:
            warnings.warn(f"An error occurred within {diff.gene2.name}! Check JSON for stack trace.")
            errors[diff.gene2.name] = traceback.format_exc()
            continue
        
        #Without gene names/evidence
        muts = [ mut.split(":")[0]for mut in mutations]
        #Gene numbers
        numbers = [
            int(mut.split("_")[0]) if "_" in mut #Indel index: <idx>_<type>_<bases>
            else 
                int(mut[:-1]) if "=" in mut #Synon SNP: <idx>=
                else int(mut[1:][:-1]) #SNP: <ref><idx><alt>
            for mut in muts
            ]

        #Iter these to pull out all other details from the GeneDifference objects
        for mut, num, full_mut in zip(muts, numbers, mutations):
            mutations_.append(full_mut) #Keep evidence in these
            genes.append(diff.gene1.name)
            gene_pos.append(num)
            codes_protein.append(diff.gene1.codes_protein and num > 0)
            is_cds.append(num > 0 and diff.gene1.codes_protein)
            is_het.append("Z" in mut.upper())
            is_null.append("X" in mut.upper())
            is_promoter.append(num < 0)
            variants.append(None)
            number_nucleotide_changes.append(diff.gene2.minor_nc_changes[num])

            if "_" in mut:
                #Indel
                if len(mut.split("_")) == 3:
                    _, t, bases = mut.split("_")
                    indel_nucleotides.append(bases)
                    if t == "del":
                        indel_length.append(-1 * len(bases))
                    else:
                        indel_length.append(len(bases))              
                else:
                    #We have a `<pos>_indel` or `<pos>_mixed`
                    indel_nucleotides.append(None)
                    indel_length.append(None)
                ref.append(None)
                alt.append(None)

                is_snp.append(False)
                nucleotide_number.append(num)
                nucleotide_index.append(diff.gene1.nucleotide_index[diff.gene1.nucleotide_number == num][0])
                aa_num.append(None)
                aa_seq.append(None)
                continue
            else:
                indel_length.append(None)
                indel_nucleotides.append(None)

            if mut[0].isupper() or mut[0] == '!':
                #Protein coding SNP
                nucleotide_number.append(None)
                nucleotide_index.append(None)
                #Pull out codons for ref/alt
                ref.append(diff.gene1.codons[diff.gene1.amino_acid_number == num][0])
                alt.append("zzz")
                is_snp.append(True)
                aa_num.append(num)
                aa_seq.append(mut[-1])
            else:
                #Other SNPs
                nucleotide_number.append(num)
                nucleotide_index.append(diff.gene1.nucleotide_index[diff.gene1.nucleotide_number == num][0])
                aa_num.append(None)
                aa_seq.append(None)
                ref.append(diff.gene1.nucleotide_sequence[diff.gene1.nucleotide_number == num][0])
                alt.append(diff.gene2.nucleotide_sequence[diff.gene2.nucleotide_number == num][0])
                is_snp.append(True)

    vals = {
        'mutation': mutations_,
        'gene': genes,
        'nucleotide_number': nucleotide_number,
        'nucleotide_index': nucleotide_index,
        'gene_position': gene_pos,
        'alt': alt,
        'ref': ref,
        'codes_protein': codes_protein,
        'indel_length': indel_length,
        'indel_nucleotides': indel_nucleotides,
        'amino_acid_number': aa_num,
        'amino_acid_sequence': aa_seq,
        'number_nucleotide_changes': number_nucleotide_changes
        }

    return pd.DataFrame(vals).astype({'mutation': 'str',
                                        'gene': 'str',
                                        'nucleotide_number': 'float',
                                        'nucleotide_index': 'float',
                                        'gene_position': 'float',
                                        'alt': 'str',
                                        'ref': 'str',
                                        'codes_protein': 'bool',
                                        'indel_length': 'float',
                                        'indel_nucleotides': 'str',
                                        'amino_acid_number': 'float',
                                        'amino_acid_sequence': 'str',
                                        'number_nucleotide_changes': 'int'
                                    }), errors

def getMutations(mutations: pd.DataFrame, catalogue: piezo.catalogue, referenceGenes: dict) -> [[str, str]]:
    '''Get all of the mutations (including multi-mutations) from the mutations df
    Multi-mutations currently only exist within the converted WHO catalogue, and are a highly specific combination 
        of mutations which must all be present for a single resistance value.

    Args:
        mutations (pd.DataFrame): Mutations dataframe
        catalogue (piezo.catalogue): The resistance catalogue. Used to find which multi-mutations we care about
        referenceGenes (dict): Dictionary of geneName->gumpy.Gene

    Returns:
        [[str, str]]: List of [gene, mutation] or in the case of multi-mutations, [None, multi-mutation]
    '''
    mutations = list(zip(mutations['gene'], mutations['mutation']))
    #Grab the multi-mutations from the catalogue
    #By doing this, we can check a fixed sample space rather than every permutation of the mutations
    #This makes the problem tractable, but does not address a possible issue with multi-mutations not encapsulating full codons
    multis = set(catalogue.catalogue.rules[catalogue.catalogue.rules['MUTATION_TYPE']=='MULTI']['MUTATION'])
    if len(multis) > 0:
        #We have a catalogue including multi rules, so check if any of these are present in the mutations
        joined = [gene+'@'+mut for (gene, mut) in mutations]
        for multi in multis:
            check = True
            for mutation in multi.split("&"):
                check = check and mutation in joined
            if check:
                #This exact multi mutation exists, so add it to the mutations list
                mutations.append((None, multi))
    
    #Check if the catalogue supports large deletions
    if "GENE" in set(catalogue.catalogue.rules['MUTATION_AFFECTS']):
        large_dels = True
    else:
        large_dels = False

    #Filtering out *just* nucleotide changes for cases of synon mutations
    #The important part of these should have already been found by multi-mutations
    fixed = []
    for gene, mutation in mutations:
        if gene is not None and referenceGenes[gene].codes_protein:
            #Codes protein so check for nucleotide changes
            nucleotide = re.compile(r"""
                                [acgtzx][0-9]+[acgtzx]
                                """, re.VERBOSE)
            if nucleotide.fullmatch(mutation):
                #Is a nucleotide (non-promoter) mutation in a coding gene
                #So skip it as it may cause prediction problems
                continue
        #Remove large dels if not supported
        if not large_dels:
            #Check if this is a large del
            large = re.compile(r"""
                                del_(1\.0)|(0\.[0-9][0-9]?[0-9]?)
                                """, re.VERBOSE)
            if large.fullmatch(mutation):
                continue
        fixed.append((gene, mutation))
    return fixed

def populateEffects(
        outputDir: str, resistanceCatalogue: piezo.ResistanceCatalogue,
        mutations: pd.DataFrame, referenceGenes: dict, vcfStem: str, make_csv: bool, make_prediction_csv: bool) -> (pd.DataFrame, dict):
    '''Populate and save the effects DataFrame as a CSV

    Args:
        outputDir (str): Path to the directory to save the CSV
        resistanceCatalogue (piezo.ResistanceCatalogue): Resistance catalogue for predictions
        mutations (pd.DataFrame): Mutations dataframe
        referenceGenes (dict): Dictionary mapping gene name --> reference gumpy.Gene objects
        vcfStem (str): The basename of the given VCF - used as the sample name
        make_csv (bool): Whether to write the CSV of the dataframe
        make_csv (bool): Whether to write the CSV of the antibiogram

    Raises:
        InvalidMutationException: Raised if an invalid mutation is detected

    Returns:
        (pd.DataFrame, dict): (DataFrame containing the effects data, A metadata dictionary mapping drugs to their predictions)
    '''
    if resistanceCatalogue is None:
        logging.debug("Catalogue was None, skipping effects and predictions generation")
        return
    #Assume wildtype behaviour unless otherwise specified
    phenotype = {drug: 'S' for drug in resistanceCatalogue.catalogue.drugs}
    phenotype_evidence = {drug: '' for drug in resistanceCatalogue.catalogue.drugs}

    effects = {}
    effectsCounter = 0

    #Default prediction values are RFUS but use piezo catalogue's values if existing
    values = resistanceCatalogue.catalogue.values

    # only try and build an effects table if there are mutations
    if mutations is not None:
        for (gene, mutation) in tqdm(getMutations(mutations, resistanceCatalogue, referenceGenes)):
            #Ensure its a valid mutation
            if gene is not None and not referenceGenes[gene].valid_variant(mutation):
                logging.error(f"Not a valid mutation {gene}@{mutation}")
                raise InvalidMutationException(gene, mutation)
            
            #Get the prediction
            if gene is not None:
                prediction = resistanceCatalogue.predict(gene+'@'+mutation, show_evidence=True)
            else:
                #This is a multi-mutation so is already of required format
                prediction = resistanceCatalogue.predict(mutation, show_evidence=True)

            #If the prediction is interesting, iter through drugs to find predictions
            if prediction != 'S':
                for drug in prediction.keys():
                    pred, evidence = prediction[drug]
                    #Prioritise values based on order within the values list
                    if values.index(pred) < values.index(phenotype[drug]):
                        #The prediction is closer to the start of the values list, so should take priority
                        phenotype[drug] = pred
                        #Track the evidence of this for the antibiogram's evidence field
                        phenotype_evidence[drug] = evidence

                    #Add to the dict
                    effects[effectsCounter] = [
                            vcfStem, gene, mutation, resistanceCatalogue.catalogue.name, 
                            drug, pred, evidence
                    ]
                    #Increment counter
                    effectsCounter += 1
        
        #Build the DataFrame
        effects = pd.DataFrame.from_dict(effects, 
                                            orient="index", 
                                            columns=["uniqueid", "gene", "mutation", 
                                                "catalogue_name", "drug", "prediction",
                                                "evidence"]
                                            )
        effects = effects[["uniqueid", "gene", "mutation", "drug", "prediction", "catalogue_name", "evidence"]]
        effects['catalogue_version'] = resistanceCatalogue.catalogue.version
        effects['prediction_values'] = ''.join(resistanceCatalogue.catalogue.values)

        #Save as CSV
        if len(effects) > 0 and make_csv:
            effects.to_csv(os.path.join(outputDir, f'{vcfStem}.effects.csv'), index=False)

        effects.reset_index(inplace=True)

    if make_prediction_csv:
        #We need to construct a simple table here
        predictions = [phenotype[drug] for drug in resistanceCatalogue.catalogue.drugs]
        vals = {
            'uniqueid': vcfStem,
            'drug': resistanceCatalogue.catalogue.drugs,
            'prediction': predictions,
            'catalogue_name': resistanceCatalogue.catalogue.name,
            'catalogue_version': resistanceCatalogue.catalogue.version,
            'catalogue_values': ''.join(resistanceCatalogue.catalogue.values),
        }
        predictions = pd.DataFrame(vals)
        predictions.to_csv(os.path.join(outputDir, f"{vcfStem}.predictions.csv"), index=False)
    #Return  the metadata dict to log later
    return effects, {"WGS_PREDICTION_"+drug: phenotype[drug] for drug in resistanceCatalogue.catalogue.drugs}

def saveJSON(variants, mutations, effects, path: str, guid: str, catalogue: piezo.ResistanceCatalogue, gnomonicusVersion: str, time_taken: float, reference: gumpy.Genome, vcf_path: str, reference_path: str, catalogue_path: str, minor_errors: dict) -> None:
    '''Create and save a single JSON output file for use within GPAS. JSON structure:
    {
        'meta': {
            'status': If this has succeeded or not (but this isn't created in cases it doesn't succeed),
            'workflow_name': 'gnomonicus',
            'workflow_task': 'resistance_prediction' or 'virulenece prediction',
            'workflow_version': gnomonicus.__version__,
            'time_taken': Time this step took,
            'UTC_timestamp': Timestamp for the end of this run,
            'catalogue_type': discrete_values or mic,
            'catalogue_name': Name of catalogue,
            'catalogue_version': Version of the catalogue,
            'reference': Name of the reference genome used,
            'catalogue_file': Path to the catalogue,
            'reference_file': Path to the reference file,
            'vcf_file': Path to the VCF file
        },
        ?'errors': {
            <gene name>: <stack trace>
        }
        'data': {
            'variants': [
                {
                    'variant': Genome level variant in GARC,
                    'nucleotide_index': Genome index of variant,
                    'gene_name': Name of the gene this variant affects (if applicable),
                    'gene_position': Gene position which this variant affects. Nucleotide number if non coding, codon indx if coding (if applicable),
                    'codon_idx': Index of the base within the corresponding codon this affects (if applicable),
                    'vcf_evidence': Parsed VCF row,
                    'vcf_idx': Which part of the VCF row to look at for this call
                }, ...
            ],
            ?'mutations': [
                {
                    'mutation': Gene level mutation in GARC,
                    'gene': Gene name,
                    'gene_position': Position within the gene. Amino acid or nucleotide index depending on which is appropriate,
                    'vcf_evidence': Parsed VCF row,
                    'ref': Ref base(s),
                    'alt': Alt base(s)
                }
            ],
            ?'effects': {
                Drug name: [
                    {
                        'gene': Gene name of the mutation,
                        'mutation': Gene level mutation in GARC,
                        'prediction': Prediction caused by this mutation,
                        'evidence': Evidence to support this prediction. Currently placeholder
                    }, ...,
                    {
                        'phenotype': Resultant prediction for this drug based on prediciton heirarchy
                    }
                ], ...
            }
            ?'antibiogram': {
                <drug> : <prediction> essentially json[data][effects][<drug>][phenotype]
            }
        }
    }
    Where fields with a preceeding '?' are not always present depending on data

    Args:
        path (str): Path to the directory where the variant/mutation/effect CSV files are saved. Also the output dir for this.
        guid (str): Sample GUID
        catalogue (piezo.ResistanceCatalogue): Catalogue used
        gnomonicusVersion (str): Semantic versioning string for the gnomonicus module. Can be accessed by `gnomonicus.__version__`
        time_taken (float): Number of seconds taken to run this.
        reference (gumpy.Genome): Reference genome object
        vcf_path (str): Path to the VCF file used for this run
        reference_path (str): Path to the reference genome used for this run
        catalogue_path (str): Path to the catalogue used for this run
        minor_errors (dict): Mapping of gene name --> stack trace of any errors occurring when parsing minor mutations
    '''
    values = catalogue.catalogue.values if catalogue is not None else list("RFUS")
    #Define some metadata for the json
    meta = {
        'status': 'success',
        'workflow_name': 'gnomonicus',
        'workflow_version': gnomonicusVersion, #gnomonicus version used
        'workflow_task': 'resistance_prediction', #TODO: Update this when we know how to detect a virulence catalogue
        'guid': guid, #Sample GUID
        'UTC-datetime-completed': datetime.datetime.utcnow().isoformat(), #ISO datetime run
        'time_taken_s': time_taken,
        'reference': reference.name,
        'catalogue_file': catalogue_path,
        'reference_file': reference_path,
        'vcf_file': vcf_path
        }
    if catalogue is not None:
        meta['catalogue_type'] = ''.join(catalogue.catalogue.values)
        meta['catalogue_name'] = catalogue.catalogue.name
        meta['catalogue_version'] = catalogue.catalogue.version
    else:
        meta['catalogue_type'] = None
        meta['catalogue_name'] = None
        meta['catalogue_version'] = None
    data = {}
    #Variants field
    _variants = []
    for _, variant in variants.iterrows():
        row = {
            'variant': variant['variant'] if pd.notnull(variant['variant']) else None,
            'nucleotide_index': variant['nucleotide_index'] if pd.notnull(variant['nucleotide_index']) else None,
            'gene_name': variant['gene'] if pd.notnull(variant['gene']) else None,
            'gene_position': variant['gene_position'] if pd.notnull(variant['gene_position']) else None,
            'codon_idx': variant['codon_idx'] if pd.notnull(variant['codon_idx']) else None,
            'vcf_evidence': json.loads(variant['vcf_evidence']),
            'vcf_idx': variant['vcf_idx'] if pd.notnull(variant['vcf_idx']) else None
        }
        _variants.append(row)
    data['variants'] = _variants

    #Depending on mutations/effects, populate
    _mutations = []
    if mutations is not None:
        for _, mutation in mutations.iterrows():
            row = {
                'mutation': mutation['mutation'] if pd.notnull(mutation['mutation']) else None,
                'gene': mutation['gene'] if pd.notnull(mutation['gene']) else None,
                'gene_position': mutation['gene_position'] if pd.notnull(mutation['gene_position']) else None,
            }
            if mutation['mutation'][0].isupper() or mutation['mutation'][0] == "!":
                #Only add codon ref/alt for AA changes
                row['ref'] = mutation['ref'] if pd.notnull(mutation['ref']) else None
                row['alt'] = mutation['alt'] if pd.notnull(mutation['alt']) else None
            _mutations.append(row)
    data['mutations'] = _mutations

    _effects = defaultdict(list)
    antibiogram = {}
    drugs = set()
    if effects is not None and len(effects) > 0:
        for _, effect in effects.iterrows():
            prediction = {
                'gene': effect['gene'] if pd.notnull(effect['gene']) else None,
                'mutation': effect['mutation'] if pd.notnull(effect['mutation']) else None,
                'prediction': effect['prediction'] if pd.notnull(effect['prediction']) else None,
                'evidence': effect['evidence']
            }
            _effects[effect['drug']].append(prediction)
        
        #Get the overall predictions for each drug
        for drug, predictions in _effects.items():
            phenotype = 'S'
            for prediction in predictions:
                #Use the prediction heierarchy to use most signifiant prediction
                if values.index(prediction['prediction']) < values.index(phenotype):
                    #The prediction is closer to the start of the values list, so should take priority
                    phenotype = prediction['prediction']
            _effects[drug].append({'phenotype': phenotype})
            antibiogram[drug] = phenotype
            drugs.add(drug)
    data['effects'] = _effects
    if catalogue is not None:
        for d in catalogue.catalogue.drugs:
            if d not in drugs:
                antibiogram[d] = "S"
    data['antibiogram'] = antibiogram

    #Convert fields to a list so it can be json serialised
    with open(os.path.join(path, f'{guid}.gnomonicus-out.json'), 'w') as f:
        #Add errors (if any)
        if len(minor_errors) > 0:
            f.write(json.dumps({'meta': meta, 'data': data, 'errors': minor_errors}, indent=2))
        else:
            f.write(json.dumps({'meta': meta, 'data': data}, indent=2))
    