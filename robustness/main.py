"""
The main file, which exposes the robustness command-line tool, detailed in
:doc:`this walkthrough <../example_usage/cli_usage>`.
"""

from argparse import ArgumentParser
import os
import git
import torch as ch
from PIL import Image

import cox
import cox.utils
import cox.store

try:
    from .model_utils import make_and_restore_model
    from .datasets import DATASETS, CustomImageNet, BinaryMNIST
    from .train import train_model, eval_model
    from .tools import constants, helpers
    from . import defaults, __version__
    from .tools.breeds_helpers import BreedsDatasetGenerator
    from . import defaults
    from .defaults import check_and_fill_args
    from .data_augmentation import get_rot_transforms
except:
    raise ValueError("Make sure to run with python -m (see README.md)")


parser = ArgumentParser()
parser = defaults.add_args_to_parser(defaults.CONFIG_ARGS, parser)
parser = defaults.add_args_to_parser(defaults.MODEL_LOADER_ARGS, parser)
parser = defaults.add_args_to_parser(defaults.TRAINING_ARGS, parser)
parser = defaults.add_args_to_parser(defaults.PGD_ARGS, parser)
parser.add_argument('--num_rots', type=int, default=0)
parser.add_argument('--num_val_rots', type=int, default=10)
parser.add_argument('--make_circ', action='store_true')
parser.add_argument('--bicubic', action='store_true')
parser.add_argument('--direct_regularizer', action='store_true')
parser.add_argument('--reg_alpha', type=float, default=0.05)
parser.add_argument('--aggregation', default='mean',
                    choices=['mean', 'max', 'softmax', 'lp'])
parser.add_argument('--p_norm', type=int, default=2)
parser.add_argument('--task', default='breeds',
                    choices=['breeds', 'binary_mnist'])


def main(args, store=None):
    '''Given arguments from `setup_args` and a store from `setup_store`,
    trains as a model. Check out the argparse object in this file for
    argument options.
    '''
    # MAKE DATASET AND LOADERS
    data_path = os.path.expandvars(args.data)

    bicubic_resample = Image.BICUBIC if args.bicubic else Image.BILINEAR
    transforms = get_rot_transforms(args.num_rots,
                                    args.num_val_rots,
                                    bicubic_resample,
                                    args.make_circ,
                                    args.task)

    if args.task == 'breeds':
        PROJ_DIR = '/home/gridsan/krisgrg/superurop/adv-rot-equiv/'
        INFO_DIR = PROJ_DIR + 'data/imagenet_class_hierarchy/modified'
        data_generator = BreedsDatasetGenerator(INFO_DIR)
        ret = data_generator.get_superclasses(level=3,
                                              Nsubclasses=None,
                                              split=None,
                                              ancestor=None,
                                              balanced=True)
        superclasses, subclass_split, _ = ret
        all_subclasses = subclass_split[0]

        # Hardcoded for BREEDS, proper mean, std computed for level=3
        dataset = CustomImageNet(data_path,
                                 custom_grouping=all_subclasses,
                                 transform_train=transforms[0],
                                 transform_test=transforms[1],
                                 mean=ch.tensor([0.486, 0.455, 0.398]),
                                 std=ch.tensor([0.221, 0.217, 0.215]))

    elif args.task == 'binary_mnist':
        dataset = BinaryMNIST(data_path,
                              transform_train=transforms[0],
                              transform_test=transforms[1])
    else:
        raise NotImplementedError('No task {args.task}.')

    train_loader, val_loader = dataset.make_loaders(
        args.workers,
        args.batch_size,
        data_aug=bool(args.data_aug))

    train_loader = helpers.DataPrefetcher(train_loader)
    val_loader = helpers.DataPrefetcher(val_loader)
    loaders = (train_loader, val_loader)

    # MAKE MODEL
    model, checkpoint = make_and_restore_model(arch=args.arch,
                                               dataset=dataset,
                                               resume_path=args.resume)
    if 'module' in dir(model):
        model = model.module

    print(args)
    if args.eval_only:
        return eval_model(args, model, val_loader, store=store)

    if not args.resume_optimizer: checkpoint = None
    model = train_model(args, model, loaders, store=store,
                                    checkpoint=checkpoint)
    return model

def setup_args(args):
    '''
    Fill the args object with reasonable defaults from
    :mod:`robustness.defaults`, and also perform a sanity check to make sure no
    args are missing.
    '''
    # override non-None values with optional config_path
    if args.config_path:
        args = cox.utils.override_json(args, args.config_path)

    ds_class = DATASETS[args.dataset]
    args = check_and_fill_args(args, defaults.CONFIG_ARGS, ds_class)

    if not args.eval_only:
        args = check_and_fill_args(args, defaults.TRAINING_ARGS, ds_class)

    if args.adv_train or args.adv_eval:
        args = check_and_fill_args(args, defaults.PGD_ARGS, ds_class)

    args = check_and_fill_args(args, defaults.MODEL_LOADER_ARGS, ds_class)
    if args.eval_only: assert args.resume is not None, \
            "Must provide a resume path if only evaluating"
    return args

def setup_store_with_metadata(args):
    '''
    Sets up a store for training according to the arguments object. See the
    argparse object above for options.
    '''
    # Add git commit to args
    try:
        repo = git.Repo(path=os.path.dirname(os.path.realpath(__file__)),
                            search_parent_directories=True)
        version = repo.head.object.hexsha
    except git.exc.InvalidGitRepositoryError:
        version = __version__
    args.version = version

    # Create the store
    store = cox.store.Store(args.out_dir, args.exp_name)
    args_dict = args.__dict__
    schema = cox.store.schema_from_dict(args_dict)
    store.add_table('metadata', schema)
    store['metadata'].append_row(args_dict)

    return store

if __name__ == "__main__":
    args = parser.parse_args()
    args = cox.utils.Parameters(args.__dict__)

    args = setup_args(args)
    store = setup_store_with_metadata(args)

    final_model = main(args, store=store)
