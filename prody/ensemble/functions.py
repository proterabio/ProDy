"""This module defines a functions for handling conformational ensembles."""

import os.path
import time
from numbers import Integral

import numpy as np

from prody.proteins import fetchPDB, parsePDB, writePDB, alignChains
from prody.utilities import openFile, showFigure, copy, isListLike, pystr
from prody import LOGGER, SETTINGS
from prody.atomic import AtomMap, Chain, AtomGroup, Selection, Segment, Select, AtomSubset
from prody.atomic.fields import DTYPE

from .ensemble import *
from .pdbensemble import *
from .conformation import *

__all__ = ['saveEnsemble', 'loadEnsemble', 'trimPDBEnsemble',
           'calcOccupancies', 'showOccupancies', 'alignPDBEnsemble',
           'buildPDBEnsemble', 'refineEnsemble']


def saveEnsemble(ensemble, filename=None, **kwargs):
    """Save *ensemble* model data as :file:`filename.ens.npz`.  If *filename*
    is **None**, title of the *ensemble* will be used as the filename, after
    white spaces in the title are replaced with underscores.  Extension is
    :file:`.ens.npz`. Upon successful completion of saving, filename is
    returned. This function makes use of :func:`~numpy.savez` function."""

    if not isinstance(ensemble, Ensemble):
        raise TypeError('invalid type for ensemble, {0}'
                        .format(type(ensemble)))
    if len(ensemble) == 0:
        raise ValueError('ensemble instance does not contain data')

    dict_ = ensemble.__dict__
    attr_list = ['_title', '_confs', '_weights', '_coords', '_indices']
    if isinstance(ensemble, PDBEnsemble):
        attr_list.append('_labels')
        attr_list.append('_trans')
    if filename is None:
        filename = ensemble.getTitle().replace(' ', '_')
    attr_dict = {}
    for attr in attr_list:
        value = dict_[attr]
        if value is not None:
            attr_dict[attr] = value

    atoms = dict_['_atoms']
    if atoms is not None:
        attr_dict['_atoms'] = np.array([atoms, None])

    data = dict_['_data']
    if len(data):
        attr_dict['_data'] = np.array([data, None])

    if isinstance(ensemble, PDBEnsemble):
        msa = dict_['_msa']
        if msa is not None:
            attr_dict['_msa'] = np.array([msa, None])

    if filename.endswith('.ens'):
        filename += '.npz'
    if not filename.endswith('.npz'):
        filename += '.ens.npz'
    ostream = openFile(filename, 'wb', **kwargs)
    np.savez(ostream, **attr_dict)
    ostream.close()
    return filename


def loadEnsemble(filename, **kwargs):
    """Returns ensemble instance loaded from *filename*.  This function makes
    use of :func:`~numpy.load` function.  See also :func:`saveEnsemble`"""

    if not 'encoding' in kwargs:
        kwargs['encoding'] = 'latin1'
    
    if not 'allow_pickle' in kwargs:
        kwargs['allow_pickle'] = True

    attr_dict = np.load(filename, **kwargs)
    if '_weights' in attr_dict:
        weights = attr_dict['_weights']
    else:
        weights = None  

    isPDBEnsemble = False

    try:
        title = attr_dict['_title']
    except KeyError:
        title = attr_dict['_name']
    if isinstance(title, np.ndarray):
        title = np.asarray(title, dtype=str)
    title = str(title)

    if weights is not None and weights.ndim == 3:
        isPDBEnsemble = True
        ensemble = PDBEnsemble(title)
    else:
        ensemble = Ensemble(title)

    ensemble.setCoords(attr_dict['_coords'])
    if '_atoms' in attr_dict:
        atoms = attr_dict['_atoms'][0]

        if isinstance(atoms, AtomGroup):
            data = atoms._data
        else:
            data = atoms._ag._data
        
        for key in data:
            arr = data[key]
            char = arr.dtype.char
            if char in 'SU' and char != DTYPE:
                arr = arr.astype(str)
                data[key] = arr
            
    else:
        atoms = None
    ensemble.setAtoms(atoms)

    if '_indices' in attr_dict:
        indices = attr_dict['_indices']
    else:
        indices = None
    ensemble._indices = indices

    if '_data' in attr_dict:
        ensemble._data = attr_dict['_data'][0]

    if isPDBEnsemble:
        confs = attr_dict['_confs']
        ensemble.addCoordset(confs, weights)
        if '_identifiers' in attr_dict.files:
            ensemble._labels = list(attr_dict['_identifiers'])
        if '_labels' in attr_dict.files:
            ensemble._labels = list(attr_dict['_labels'])
        if ensemble._labels:
            for i, label in enumerate(ensemble._labels):
                if not isinstance(label, str):
                    try:
                        ensemble._labels[i] = label.decode()
                    except AttributeError:
                        ensemble._labels[i] = str(label)
        if '_trans' in attr_dict.files:
            ensemble._trans = attr_dict['_trans']
        if '_msa' in attr_dict.files:
            ensemble._msa = attr_dict['_msa'][0]
    else:
        ensemble.addCoordset(attr_dict['_confs'])
        if weights is not None:
            ensemble.setWeights(weights)
    return ensemble


def trimPDBEnsemble(pdb_ensemble, occupancy=None, **kwargs):
    """Returns a new PDB ensemble obtained by trimming given *pdb_ensemble*.
    This function helps selecting atoms in a pdb ensemble based on one of the
    following criteria, and returns them in a new :class:`.PDBEnsemble`
    instance.

    Resulting PDB ensemble will contain atoms whose occupancies are greater
    or equal to *occupancy* keyword argument. Occupancies for atoms will be
    calculated using ``calcOccupancies(pdb_ensemble, normed=True)``.

    :arg occupancy: occupancy for selecting atoms, must satisfy
        ``0 < occupancy <= 1``.
        If set to *None* then *hard* trimming will be performed.
    :type occupancy: float

    :arg hard: Whether to perform hard trimming.
        Default is **False**
        If set to **True**, atoms will be completely removed from *pdb_ensemble*.
        If set to **False**, a soft trimming of *pdb_ensemble* will be done
        where atoms will be removed from the active selection. This is useful, 
        for example, when one uses :func:`calcEnsembleENMs` 
        together with :func:`sliceModel` or :func:`reduceModel`
        to calculate the modes from the remaining part while still taking the 
        removed part into consideration (e.g. as the environment).
    :type hard: bool
    """

    hard = kwargs.pop('hard', False) or pdb_ensemble._atoms is None \
           or occupancy is None

    atoms = pdb_ensemble.getAtoms(selected=hard)

    if not isinstance(pdb_ensemble, PDBEnsemble):
        raise TypeError('pdb_ensemble argument must be a PDBEnsemble')
    if pdb_ensemble.numConfs() == 0 or pdb_ensemble.numAtoms() == 0:
        raise ValueError('pdb_ensemble must have conformations')

    if occupancy is not None:
        occupancy = float(occupancy)
        assert 0 < occupancy <= 1, ('occupancy is not > 0 and <= 1: '
                                    '{0}'.format(repr(occupancy)))
        n_confs = pdb_ensemble.numConfs()
        assert n_confs > 0, 'pdb_ensemble does not contain any conformations'
        occupancies = calcOccupancies(pdb_ensemble, normed=True)
        #assert weights is not None, 'weights must be set for pdb_ensemble'
        #weights = weights.flatten()
        #mean_weights = weights / n_confs
        torf = occupancies >= occupancy
    else:
        n_atoms = pdb_ensemble.getCoords().shape[0]
        torf = np.ones(n_atoms, dtype=bool)

    trimmed = PDBEnsemble(pdb_ensemble.getTitle())
    if hard:
        if atoms is not None:
            trim_atoms_idx = [n for n,t in enumerate(torf) if t]
            trim_atoms = atoms[trim_atoms_idx]
            trimmed.setAtoms(trim_atoms)

        coords = pdb_ensemble.getCoords()
        if coords is not None:
            trimmed.setCoords(coords[torf])
        confs = pdb_ensemble.getCoordsets()
        if confs is not None:
            weights = pdb_ensemble.getWeights()
            labels = pdb_ensemble.getLabels()
            msa = pdb_ensemble.getMSA()
            if msa:
                msa = msa[:, torf]
            trimmed.addCoordset(confs[:, torf], weights[:, torf], labels, sequence=msa)
    else:
        indices = np.where(torf)[0]
        selids = pdb_ensemble._indices

        if selids is not None:
            indices = selids[indices]

        select = atoms[indices]
        trimmed.setAtoms(atoms)
        trimmed.setAtoms(select)

        coords = copy(pdb_ensemble._coords)
        if coords is not None:
            trimmed.setCoords(coords)
        confs = copy(pdb_ensemble._confs)
        if confs is not None:
            weights = copy(pdb_ensemble._weights)
            labels = pdb_ensemble.getLabels()
            msa = pdb_ensemble._msa
            trimmed.addCoordset(confs, weights, labels, sequence=msa)

        trimmed.setAtoms(select)

    trimmed._data = pdb_ensemble._data
    return trimmed

def calcOccupancies(pdb_ensemble, normed=False):
    """Returns occupancy calculated from weights of a :class:`.PDBEnsemble`.
    Any non-zero weight will be considered equal to one.  Occupancies are
    calculated by binary weights for each atom over the conformations in
    the ensemble. When *normed* is **True**, total weights will be divided
    by the number of atoms.  This function can be used to see how many times
    a residue is resolved when analyzing an ensemble of X-ray structures."""

    if not isinstance(pdb_ensemble, PDBEnsemble):
        raise TypeError('pdb_ensemble must be a PDBEnsemble instance')
    if len(pdb_ensemble) == 0:
        raise ValueError('pdb_ensemble does not contain any conformations')
    assert isinstance(normed, bool), 'normed must be a boolean'
    weights = pdb_ensemble.getWeights()
    if weights is None:
        raise ValueError('pdb_ensemble weights are not set')

    occupancies = weights.astype(bool).sum(0).astype(float).flatten()
    if normed:
        return occupancies / len(pdb_ensemble)
    else:
        return occupancies


def showOccupancies(pdbensemble, *args, **kwargs):
    """Show occupancies for the PDB ensemble using :func:`~matplotlib.pyplot.
    plot`.  Occupancies are calculated using :meth:`calcOccupancies`."""

    import matplotlib.pyplot as plt

    normed = kwargs.pop('normed', False)

    if not isinstance(pdbensemble, PDBEnsemble):
        raise TypeError('pdbensemble must be a PDBEnsemble instance')
    weights = calcOccupancies(pdbensemble, normed)
    if weights is None:
        return None
    show = plt.plot(weights, *args, **kwargs)
    axis = list(plt.axis())
    axis[2] = 0
    axis[3] += 1
    plt.axis(axis)
    plt.xlabel('Atom index')
    plt.ylabel('Sum of weights')
    if SETTINGS['auto_show']:
        showFigure()
    return show

def alignPDBEnsemble(ensemble, suffix='_aligned', outdir='.', gzip=False):
    """Align PDB files using transformations from *ensemble*, which may be
    a :class:`.PDBEnsemble` or a :class:`.PDBConformation` instance. Label of
    the conformation (see :meth:`~.PDBConformation.getLabel`) will be used to
    determine the PDB structure and model number.  First four characters of
    the label is expected to be the PDB identifier and ending numbers to be the
    model number.  For example, the :class:`.Transformation` from conformation
    with label *2k39_ca_selection_'resnum_<_71'_m116* will be applied to 116th
    model of structure **2k39**.  After applicable transformations are made,
    structure will be written into *outputdir* as :file:`2k39_aligned.pdb`.
    If ``gzip=True``, output files will be compressed.  Return value is
    the output filename or list of filenames, in the order files are processed.
    Note that if multiple models from a file are aligned, that filename will
    appear in the list multiple times."""

    if not isinstance(ensemble, (PDBEnsemble, PDBConformation)):
        raise TypeError('ensemble must be a PDBEnsemble or PDBConformation')
    if isinstance(ensemble, PDBConformation):
        ensemble = [ensemble]
    if gzip:
        gzip = '.gz'
    else:
        gzip = ''
    output = []
    pdbdict = {}
    for conf in ensemble:
        trans = conf.getTransformation()
        if trans is None:
            raise ValueError('transformations are not calculated, call '
                             '`superpose` or `iterpose`')
        label = conf.getLabel()

        pdb = label[:4]
        filename = pdbdict.get(pdb, fetchPDB(pdb))
        if filename is None:
            LOGGER.warning('PDB file for conformation {0} is not found.'
                           .format(label))
            output.append(None)
            continue
        LOGGER.info('Parsing PDB file {0} for conformation {1}.'
                    .format(pdb, label))

        acsi = None
        model = label.rfind('m')
        if model > 3:
            model = label[model+1:]
            if model.isdigit():
                acsi = int(model) - 1
            LOGGER.info('Applying transformation to model {0}.'
                        .format(model))

        if isinstance(filename, str):
            ag = parsePDB(filename)
        else:
            ag = filename

        if acsi is not None:
            if acsi >= ag.numCoordsets():
                LOGGER.warn('Model number {0} for {1} is out of range.'
                            .format(model, pdb))
                output.append(None)
                continue
            ag.setACSIndex(acsi)
        trans.apply(ag)
        outfn = os.path.join(outdir, pdb + suffix + '.pdb' + gzip)
        if ag.numCoordsets() > 1:
            pdbdict[pdb] = ag
        else:
            writePDB(outfn, ag)
        output.append(os.path.normpath(outfn))

    for pdb, ag in pdbdict.items():  # PY3K: OK
        writePDB(os.path.join(outdir, pdb + suffix + '.pdb' + gzip), ag)
    if len(output) == 1:
        return output[0]
    else:
        return output


def buildPDBEnsemble(atomics, ref=None, title='Unknown', labels=None, unmapped=None, **kwargs):
    """Builds a :class:`.PDBEnsemble` from a given reference structure and a list of structures 
    (:class:`.Atomic` instances). Note that the reference should be included in the list as well.

    :arg atomics: a list of :class:`.Atomic` instances
    :type atomics: list

    :arg ref: reference structure or the index to the reference in *atomics*. If **None**,
        then the first item in *atomics* will be considered as the reference. If it is a 
        :class:`.PDBEnsemble` instance, then *atomics* will be appended to the existing ensemble.
        Default is **None**
    :type ref: int, :class:`.Chain`, :class:`.Selection`, or :class:`.AtomGroup`

    :arg title: the title of the ensemble
    :type title: str

    :arg labels: labels of the conformations
    :type labels: list

    :arg degeneracy: whether only the active coordinate set (**True**) or all the coordinate sets 
        (**False**) of each structure should be added to the ensemble. Default is **True**
    :type degeneracy: bool

    :arg occupancy: minimal occupancy of columns (range from 0 to 1). Columns whose occupancy
        is below this value will be trimmed
    :type occupancy: float

    :arg unmapped: labels of *atomics* that cannot be included in the ensemble. This is an 
        output argument
    :type unmapped: list

    :arg subset: a subset for selecting particular atoms from the input structures.
        Default is ``"calpha"``
    :type subset: str

    :arg superpose: if set to ``'iter'``, :func:`.PDBEnsemble.iterpose` will be used to 
        superpose the structures, otherwise conformations will be superposed with respect 
        to the reference specified by *ref*. Default is ``'iter'``
    :type superpose: str
    """

    occupancy = kwargs.pop('occupancy', None)
    degeneracy = kwargs.pop('degeneracy', True)
    subset = str(kwargs.get('subset', 'calpha')).lower()
    superpose = kwargs.pop('superpose', 'iter')
    superpose = kwargs.pop('iterpose', superpose)
    debug = kwargs.pop('debug', {})

    if 'mapping_func' in kwargs:
        raise DeprecationWarning('mapping_func is deprecated. Please see release notes for '
                                 'more details: http://prody.csb.pitt.edu/manual/release/v1.11_series.html')
    start = time.time()

    if len(atomics) == 1:
        raise ValueError('atomics should have at least two items')

    if labels is not None:
        if len(labels) != len(atomics):
            raise TypeError('Labels and atomics must have the same lengths.')
    else:
        labels = []
        
        for atoms in atomics:
            if atoms is None:
                labels.append(None)
            else:
                labels.append(atoms.getTitle())

    if ref is None:
        target = atomics[0]
    elif isinstance(ref, Integral):
        target = atomics[ref]
    elif isinstance(ref, PDBEnsemble):
        target = ref._atoms
    else:
        target = ref
    
    # initialize a PDBEnsemble with reference atoms and coordinates
    if isinstance(ref, PDBEnsemble):
        ensemble = ref
    else:
        # select the subset of reference beforehand for the sake of efficiency
        if subset != 'all':
            target = target.select(subset)
        ensemble = PDBEnsemble(title)
        ensemble.setAtoms(target)
        ensemble.setCoords(target.getCoords())
    
    # build the ensemble
    if unmapped is None: unmapped = []

    LOGGER.progress('Building the ensemble...', len(atomics), '_prody_buildPDBEnsemble')
    for i, atoms in enumerate(atomics):
        if atoms is None:
            unmapped.append(labels[i])
            continue

        LOGGER.update(i, 'Mapping %s to the reference...'%atoms.getTitle(), 
                      label='_prody_buildPDBEnsemble')
        try:
            atoms.getHierView()
        except AttributeError:
            raise TypeError('atomics must be a list of instances having the access to getHierView')
        
        if subset != 'all':
            atoms = atoms.select(subset)

        # find the mapping of chains of atoms to those of target
        debug[labels[i]] = {}
        atommaps = alignChains(atoms, target, debug=debug[labels[i]], **kwargs)

        if len(atommaps) == 0:
            unmapped.append(labels[i])
            continue
        
        # add the atommaps to the ensemble
        for atommap in atommaps:
            lbl = pystr(labels[i])
            if len(atommaps) > 1:
                chids = np.unique(atommap.getChids())
                strchids = ''.join(chids)
                lbl += '_%s'%strchids
            ensemble.addCoordset(atommap, weights=atommap.getFlags('mapped'), 
                                label=lbl, degeneracy=degeneracy)

    LOGGER.finish()

    if occupancy is not None:
        ensemble = trimPDBEnsemble(ensemble, occupancy=occupancy)

    if superpose != 'iter':
        ensemble.superpose()
    else:
        ensemble.iterpose()
    
    LOGGER.info('Ensemble ({0} conformations) were built in {1:.2f}s.'
                     .format(ensemble.numConfs(), time.time()-start))

    if unmapped:
        LOGGER.warn('{0} structures cannot be mapped.'.format(len(unmapped)))
    return ensemble

def refineEnsemble(ensemble, lower=.5, upper=10., **kwargs):
    """Refine a :class:`.PDBEnsemble` based on RMSD criterions.
    
    :arg ensemble: the ensemble to be refined
    :type ensemble: :class:`.Ensemble`, :class:`.PDBEnsemble`

    :arg lower: the smallest allowed RMSD between two conformations with the exception of **protected** 
    :type lower: float

    :arg upper: the highest allowed RMSD between two conformations with the exception of **protected** 
    :type upper: float

    :keyword protected: a list of either the indices or labels of the conformations needed to be kept 
                        in the refined ensemble
    :type protected: list
    
    :arg ref: the index or label of the reference conformation which will also be kept.
        Default is 0
    :type ref: int or str
    """ 

    protected = kwargs.pop('protected', [])
    P = []
    if len(protected):
        labels = ensemble.getLabels()
        for p in protected:
            if isinstance(p, Integral):
                i = p
            else:
                if p in labels:
                    i = labels.index(p)
                else:
                    LOGGER.warn('could not find any conformation with the label %s in the ensemble'%str(p))
            P.append(i)

    LOGGER.timeit('_prody_refineEnsemble')
    from numpy import argsort

    ### obtain reference index
    # rmsd = ensemble.getRMSDs()
    # ref_i = np.argmin(rmsd)
    ref_i = kwargs.pop('ref', 0)
    if isinstance(ref_i, Integral):
        pass
    elif isinstance(ref_i, str):
        labels = ensemble.getLabels()
        ref_i = labels.index(ref_i)
    else:
        LOGGER.warn('could not find any conformation with the label %s in the ensemble'%str(ref_i))
    if not ref_i in P:
        P = [ref_i] + P

    ### calculate pairwise RMSDs ###
    RMSDs = ensemble.getRMSDs(pairwise=True)

    def getRefinedIndices(A):
        deg = A.sum(axis=0)
        sorted_indices = list(argsort(deg))
        # sorted_indices = P + [x for x in sorted_indices if x not in P]
        sorted_indices.remove(ref_i)
        sorted_indices.insert(0, ref_i)

        n_confs = ensemble.numConfs()
        isdel_temp = np.zeros(n_confs)
        for a in range(n_confs):
            i = sorted_indices[a]
            for b in range(n_confs):
                if a >= b:
                    continue
                j = sorted_indices[b]
                if isdel_temp[i] or isdel_temp[j] :
                    continue
                else:
                    if A[i,j]:
                        # isdel_temp[j] = 1
                        if not j in P:
                            isdel_temp[j] = 1
                        elif not i in P:
                            isdel_temp[i] = 1
        temp_list = isdel_temp.tolist()
        ind_list = []
        for i in range(n_confs):
            if not temp_list[i]:
                ind_list.append(i)
        return ind_list

    L = list(range(len(ensemble)))
    U = list(range(len(ensemble)))
    if lower is not None:
        A = RMSDs < lower
        L = getRefinedIndices(A)

    if upper is not None:
        B = RMSDs > upper
        U = getRefinedIndices(B)
    
    # find common indices from L and U
    I = list(set(L) - (set(L) - set(U)))

    # for p in P:
        # if p not in I:
            # I.append(p)
    I.sort()
    reens = ensemble[I]

    LOGGER.report('Ensemble was refined in %.2fs.', '_prody_refineEnsemble')
    LOGGER.info('%d conformations were removed from ensemble.'%(len(ensemble) - len(I)))

    return reens
