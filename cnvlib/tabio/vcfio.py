"""Variant Call Format (VCF) for SNV loci."""
from __future__ import absolute_import, division, print_function
# from past.builtins import basestring

import collections
import logging
from itertools import chain

import pandas as pd
import numpy as np
import pysam

from ..vary import VariantArray as VA


def read_vcf(infile, sample_id=None, normal_id=None,
             min_depth=None, skip_reject=False, skip_somatic=False):
    """Read one tumor-normal pair or unmatched sample from a VCF file.

    By default, return the first tumor-normal pair or unmatched sample in the
    file.  If `sample_id` is a string identifier, return the (paired or single)
    sample  matching that ID.  If `sample_id` is a positive integer, return the
    sample or pair at that index position, counting from 0.
    """
    # if isinstance(infile, basestring):
    #     vcf_reader = vcf.Reader(filename=infile)
    # else:
    #     vcf_reader = vcf.Reader(infile)
    try:
        vcf_reader = pysam.VariantFile(infile)
    except Exception as exc:
        raise ValueError("Must give a VCF filename, not open file handle: %s"
                         % exc)
    if not vcf_reader.header.samples:
        logging.warn("VCF file %s has no samples; parsing minimal info", infile)
        # return sample_id, normal_id, _read_vcf_nosample(infile, skip_reject)
        return _read_vcf_nosample(infile, skip_reject)

    sid, nid = _choose_samples(vcf_reader, sample_id, normal_id)
    logging.info("Selected test sample " + str(sid) +
                 (" and control sample %s" % nid if nid else ''))
    # NB: in-place
    vcf_reader.subset_samples(list(filter(None, (sid, nid))))


    columns = ['chromosome', 'start', 'end', 'ref', 'alt', 'somatic',
               'zygosity', 'depth', 'alt_count']
    if nid:
        columns.extend(['n_zygosity', 'n_depth', 'n_alt_count'])

    rows = _parse_records(vcf_reader, sid, nid, skip_reject)
    table = pd.DataFrame.from_records(rows, columns=columns)
    table['alt_freq'] = table['alt_count'] / table['depth']
    if nid:
        table['n_alt_freq'] = table['n_alt_count'] / table['n_depth']
    table = table.fillna({col: 0.0 for col in table.columns[6:]})
    # Filter out records as requested
    cnt_depth = cnt_som = 0
    if min_depth:
        if table['depth'].any():
            dkey = 'n_depth' if 'n_depth' in table else 'depth'
            idx_depth = table[dkey] >= min_depth
            cnt_depth = (~idx_depth).sum()
            table = table[idx_depth]
        else:
            logging.warn("Depth info not available for filtering")
    if skip_somatic:
        idx_som = table['somatic']
        cnt_som = idx_som.sum()
        table = table[~idx_som]
    logging.info("Loaded %d records; skipped: %d somatic, %d depth",
                 len(table), cnt_som, cnt_depth)
    # return sid, nid, table
    return table


def _read_vcf_nosample(vcf_file, skip_reject=False):
    columns = ['chromosome', 'start', 'ref', 'alt', # 'filter', 'info',
              ]
    dtypes = [str, int, str, str, # str, str
             ]
    table = pd.read_table(vcf_file,
                          comment="#",
                          header=None,
                          na_filter=False,
                          names=["chromosome", "start", "_ID", "ref", "alt",
                                 "_QUAL", "filter", "info"],
                          usecols=columns,
                          # ENH: converters={'info': func to parse it}
                          dtype=dict(zip(columns, dtypes)),
                         )
    # ENH: do things with filter, info
    # if skip_reject and record.FILTER and len(record.FILTER) > 0:
    table['end'] = table['start'] + table["alt"].str.len()  # ENH: INFO["END"]
    table['start'] -= 1
    logging.info("Loaded %d plain records", len(table))
    return table.loc[:, VA._required_columns]


def _choose_samples(vcf_reader, sample_id, normal_id):
    """Emit the sample IDs of all samples or tumor-normal pairs in the VCF.

    Determine tumor-normal pairs from the PEDIGREE tag(s). If no PEDIGREE tag is
    present, use the specified sample_id and normal_id as the pair, or if
    unspecified, emit all samples as unpaired tumors.
    """
    vcf_samples = list(vcf_reader.header.samples)
    if isinstance(sample_id, int):
        sample_id = vcf_samples[sample_id]
    if isinstance(normal_id, int):
        normal_id = vcf_samples[normal_id]
    for sid in (sample_id, normal_id):
        if sid and sid not in vcf_samples:
            raise IndexError("Specified sample %s not in VCF file"
                             % sid)
    pairs = None
    peds = list(_parse_pedigrees(vcf_reader))
    if peds:
        # Trust the PEDIGREE tag
        pairs = peds
    elif normal_id:
        # All/any other samples are tumors paired with this normal
        try:
            other_ids = [s for s in vcf_samples if s != normal_id]
        except StopIteration:
            raise IndexError(
                "No other sample in VCF besides the specified normal " +
                normal_id + "; did you mean to use this as the sample_id "
                "instead?")
        pairs = [(oid, normal_id) for oid in other_ids]
    else:
        # All samples are unpaired tumors
        pairs = [(sid, None) for sid in vcf_samples]
    if sample_id:
        # Keep only the specified tumor/test sample
        pairs = [(s, n) for s, n in pairs if s == sample_id]
    if not pairs:
        # sample_id refers to a normal/control sample -- salvage it
        pairs = [(sample_id, None)]
    for sid in set(chain(*pairs)) - {None}:
        _confirm_unique(sid, vcf_samples)

    sid, nid = pairs[0]
    if len(pairs) > 1:
        if nid :
            logging.warn("WARNING: VCF file contains multiple tumor-normal "
                         "pairs; returning the first pair '%s' / '%s'",
                         sid, nid)
        else:
            logging.warn("WARNING: VCF file contains multiple samples; "
                         "returning the first sample '%s'", sid)

    return sid, nid


def _parse_pedigrees(vcf_reader):
    """Extract tumor/normal pair sample IDs from the VCF header.

    Return an iterable of (tumor sample ID, normal sample ID).
    """
    meta = collections.defaultdict(list)
    for hr in vcf_reader.header.records:
        if hr.key and hr.key not in ('ALT', 'FILTER', 'FORMAT', 'INFO', 'contig'):
            meta[hr.key].append(dict(hr.items()))
    # Prefer the standard tag
    if "PEDIGREE" in meta:
        for tag in meta["PEDIGREE"]:
            if "Derived" in tag:
                sample_id = tag["Derived"]
                normal_id = tag["Original"]
                logging.debug("Found tumor sample %s and normal sample %s "
                              "in the VCF header PEDIGREE tag",
                              sample_id, normal_id)
                yield sample_id, normal_id
    # GATK Mutect and Mutect2 imply paired tumor & normal IDs
    elif "GATKCommandLine" in meta:
        # GATK 3.0(?) and earlier
        for tag in meta["GATKCommandLine"]:
            if tag.get("ID") == "MuTect":  # any others OK?
                options = dict(kv.split("=", 1)
                               for kv in (tag["CommandLineOptions"]
                                          .strip('"').split())
                               if '=' in kv)
                sample_id = options.get('tumor_sample_name')
                normal_id = options['normal_sample_name']
                logging.debug("Found tumor sample %s and normal sample "
                              "%s in the MuTect VCF header",
                              sample_id, normal_id)
                yield sample_id, normal_id
    elif "GATKCommandLine.MuTect2" in meta:
        # GATK 3+ metadata is suboptimal. Apparent convention:
        # Tumor is the first sample, normal is the second.
        yield tuple(vcf_reader.header.samples)


def _confirm_unique(sample_id, samples):
    occurrences = [s for s in samples if s == sample_id]
    if len(occurrences) != 1:
        raise IndexError(
            "Did not find a single sample ID '%s' in: %s"
            % (sample_id, samples))


def _parse_records(records, sample_id, normal_id, skip_reject):
    """Parse VCF records into DataFrame rows.

    Apply filters to skip records with low depth, homozygosity, the REJECT
    flag, or the SOMATIC info field.
    """
    cnt_reject = 0  # For logging
    for record in records:
        is_som = False
        if (skip_reject and record.filter and len(record.filter) > 0
            and len(set(record.filter) - {'PASS', '.'})):
            cnt_reject += 1
            continue
        if record.info.get("SOMATIC"):
            is_som = True

        sample = record.samples[sample_id]
        try:
            depth, zygosity, alt_count = _extract_genotype(sample)
            if normal_id:
                normal = record.samples[normal_id]
                n_depth, n_zygosity, n_alt_count = _extract_genotype(normal)
                if n_zygosity == 0:
                    is_som = True
        # if alt_count is np.nan:
        except Exception as exc:
            logging.error("Skipping %s:%d %s @ %s; %s",
                          record.chrom, record.pos, record.ref, sample.name, exc)
            raise

        # Split multiallelics?
        # XXX Ensure sample genotypes are handled properly
        start = record.start
        for alt in record.alts:
            if alt == '<NON_REF>':
                # gVCF placeholder -- not a real allele
                continue
            end = _get_end(start, alt, record.info)
            row = (record.chrom, start, end, record.ref, alt,
                   is_som, zygosity, depth, alt_count)
            if normal_id:
                row += (n_zygosity, n_depth, n_alt_count)
            yield row

    if cnt_reject:
        logging.info('Filtered out %d records', cnt_reject)


def _extract_genotype(sample):
    if 'DP' in sample:
        depth = sample['DP']
    elif 'AD' in sample and isinstance(sample['AD'], tuple):
        depth = _safesum(sample['AD'])
    else:
        # SV or not called, probably
        depth = np.nan  #0.0
    gts = set(sample['GT'])
    if len(gts) > 1:
        zygosity = 0.5
    elif gts.pop() == 0:
        zygosity = 0.0
    else:
        zygosity = 1.0
    alt_count = _get_alt_count(sample)
    return depth, zygosity, alt_count


def _get_alt_count(sample):
    """Get the alternative allele count from a sample in a VCF record."""
    if sample.get('AD') not in (None, (None,)):
        # GATK and other callers: (ref depth, alt depth)
        if isinstance(sample['AD'], tuple):
            alt_count = sample['AD'][1]
        # VarScan
        else:
            alt_count = sample['AD']
    elif sample.get('CLCAD2') not in (None, (None,)):
        # Qiagen CLC Genomics Server -- similar to GATK's AD
        alt_count = sample['CLCAD2'][1]
    elif 'AO' in sample:
        if isinstance(sample['AO'], tuple):
            alt_count = _safesum(sample['AO'])
        elif sample['AO']:
            alt_count = sample['AO']
        else:
            alt_count = 0.0
    else:
        alt_count = np.nan
    return alt_count


def _safesum(tup):
    return sum(filter(None, tup))


def _get_end(posn, alt, info):
    """Get record end position."""
    if "END" in info:
        # Structural variant
        return info['END']
    return posn + len(alt)

# _____________________________________________________________________

def write_vcf(dframe):
    """Variant Call Format (VCF) for SV loci."""
    return NotImplemented
    # See export.export_vcf()
