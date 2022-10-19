import os
import sys
import click
import uuid
import time
import numpy as np
from functools import partial

from src.train import train_model, run_epoch, load_model
from src.plotting import Visualizer, VisualizerGridsearch

from src.gridsearch import generate_gridsearch_worker_params, get_gridsearch_default_scans, results_to_df
from src.gridsearch import wrap_command_with_local, wrap_command_with_slurm, write_jobs
from src.utils import command_with_config, ensure_dir, get_command_defaults
from src.utils import update_yaml, write_yaml, read_yaml, timestamp, strtuple_to_list, str_to_list, get_last_config
from src.data import SystemFamily, sindy_library, load_dataset
from sklearn.model_selection import train_test_split, KFold
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import classification_report

import torch

# Here we will monkey-patch click Option __init__
# in order to force showing default values for all options
orig_init = click.core.Option.__init__


def new_init(self, *args, **kwargs):
    orig_init(self, *args, **kwargs)
    self.show_default = True


click.core.Option.__init__ = new_init  # type: ignore


@click.group()
@click.version_option()
def cli():
    pass

################################################## Single Experiment ###################################################

@cli.command(name="generate-dataset", cls=command_with_config('config_file'), context_settings=dict(ignore_unknown_options=True, allow_extra_args=True), 
help="Generates a data set of vector fields.")
@click.option('--data-dir', '-f', type=str)
@click.option('--data-set-name', '-d', type=str, default='dataset')
@click.option('--system-names', '-s', type=str, multiple=True, default=['simple_oscillator'])
@click.option('--num-samples', '-m', type=int, default=1000)
@click.option('--samplers', '-sp', type=str, multiple=True, default=['uniform'])
@click.option('--system-props', '-c', type=float, multiple=True, default=[1.0])
@click.option('--val-size', '-t', type=float, default=.25)
@click.option('--num_lattice', '-n', type=int, default=64)
@click.option('--min-dims', '-mi', type=list, default=[-1.,-1.])
@click.option('--max-dims', '-ma', type=list, default=[1.,1.])
@click.option('--config-file', type=click.Path())
def generate_dataset(data_dir, data_set_name, system_names, num_samples, samplers, system_props, val_size, num_lattice, min_dims, max_dims, config_file):
    """
    Generates train and test data for one data set

    Positional arguments:

    data_set_name (str): name of data set and folder to save all data in.
    system_names (list of str): names of data to generate
    num_samples (int): number of total samples to generate
    samplers (list of strings): for each system, a string denoting the type of sampler used. 
    system_props (list of floats): for each system, a float controlling proportion of total data this system will comprise.
    val_size (float): proportion in (0,1) of data allocated to validation set
    num_lattice (int): number of points for all dimensions in the equally spaced grid on which velocity is measured
    min_dims (list of floats): the lower bounds for each dimension in phase space
    max_dims (list of floats): the upper bounds for each dimension in phase space

    """

    # Living dangerously
    import warnings
    warnings.filterwarnings("ignore")
    # TODO: For now, no control here over param ranges, min or max dims. Just use sf defaults
    # TODO: Add noise params
    # TODO: Add control for poly params

    save_dir = os.path.join(data_dir, data_set_name)
    ensure_dir(save_dir)

    all_data   = []
    all_labels = []
    all_pars   = []

    sfs = [SystemFamily(data_name=system_name, default_sampler=sampler, num_lattice=num_lattice, min_dims=min_dims, max_dims=max_dims) for (system_name, sampler) in zip(system_names, samplers)]
    num_labels_per_group = [len(sf.param_groups) for sf in sfs]
    cum_labels_per_group = [0] + list(np.cumsum(num_labels_per_group))[:-1]

    # For each system
    for d, system_name in enumerate(system_names):

        print(f'Generating {system_name} data.')

        sampler       = samplers[d]
        sf            = sfs[d]
        num_classes   = sf.num_classes

        system_samples = int(system_props[d] * num_samples)
        class_samples  = int(system_samples / float(num_classes))

        # For each class in the system
        for c in range(num_classes):
            current_exemplars = 0
            # Until you have the right number of exemplars from this class
            while current_exemplars < class_samples:
                gen_par = sf.param_sampler(1)[0]
                system  = sf.generate_model(gen_par)
                if system.label != c:
                    # If wrong label, get another sample
                    continue
                else:
                    current_exemplars += 1
                    datum     = system.forward(0,sf.L)
                    label     = system.label + cum_labels_per_group[d]

                    # If system doesn't have a closed from in the dictionary, approximate its coefficients with least squares
                    if system_name in ['simple_oscillator', 'alon', 'conservative', 'incompressible']:
                        dx, dy = system.fit_polynomial_representation(poly_order=3)
                    else:
                        dx, dy = system.get_polynomial_representation()
                    save_pars = torch.cat((torch.tensor(dx.to_numpy()), torch.tensor(dy.to_numpy()))).transpose(1,0).float()

                    all_data.append(datum)
                    all_pars.append(save_pars)
                    all_labels.append(label)

    all_data   = torch.stack(all_data).numpy().transpose(0,3,1,2)
    all_pars   = torch.stack(all_pars).numpy()
    all_labels = np.array(all_labels)

    split = train_test_split(all_data, all_labels, all_pars, test_size=val_size, stratify=all_labels)

    for dt, nm in zip(split, ['X_train', 'X_val', 'y_train', 'y_val', 'p_train', 'p_val']):
        np.save(os.path.join(save_dir, nm + '.npy'), dt)

@cli.command(name='train', cls=command_with_config('config_file'), help='train a VAE to learn reduced models')
@click.argument("data-config", type=click.Path())
@click.argument("net-config", type=click.Path())
@click.option('--exp-name', type=str)
@click.option('--num-epochs', type=int, default=10)
@click.option('--batch-size', type=int, default=64)
@click.option('--beta', type=float, default=1.0)
@click.option('--fp_normalize', is_flag=True, default=True)
@click.option('--device', type=str, default='cpu')
@click.option('--optimizer', type=str, default='Adam')
@click.option('--learning-rate', type=float, default=.0001)
@click.option('--momentum', type=float, default=0.0)
@click.option('--model-save-dir', type=str)
@click.option('--log-dir', type=str)
@click.option('--log-period', type=int, default=10)
@click.option('--seed', type=int, default=0)
@click.option('--config-file', type=click.Path())
def call_train(data_config, net_config, exp_name, num_epochs, batch_size, beta, fp_normalize, poly_order, device, optimizer, learning_rate, momentum, model_save_dir, log_dir, log_period, seed, config_file):
    """
    Train vector field embeddings
    """
    data_info = read_yaml(data_config)
    net_info = read_yaml(net_config)

    if exp_name is None:
        id = str(uuid.uuid4())
        exp_name = sf.data_name + '_' + id

    ensure_dir(log_dir)
    model_save_dir = os.path.join(model_save_dir, exp_name)
    ensure_dir(model_save_dir)

    start = time.time()

    data_path = os.path.join(data_info['data_dir'], data_info['data_set_name'])

    X_train, X_test, y_train, y_test, p_train, p_test = load_dataset(data_path)

    model_type = net_info['model_type']
    pretrained_path = net_info['pretrained_path']
    del net_info['model_type']
    del net_info['pretrained_path']
    net = load_model(model_type, pretrained_path=pretrained_path, device=device, **net_info)

    net = train_model(X_train, X_test,
                      y_train, y_test,
                      p_train, p_test,
                      net, exp_name,
                      num_epochs=num_epochs,
                      learning_rate=learning_rate,
                      momentum=momentum,
                      optimizer=optimizer,
                      batch_size=batch_size,
                      beta=beta,
                      fp_normalize=fp_normalize,
                      device=device,
                      log_dir=log_dir,
                      log_period=log_period)

    torch.save(net.state_dict(), os.path.join(model_save_dir, 'model.pt'))

@cli.command(name="evaluate", help='Evaluates a trained model on a data set.')
@click.argument('data-config', type=str)
@click.argument('net-config', type=str)
@click.argument('train-config', type=str)
@click.option('--pretrained-path', type=str)
@click.option('--results-dir', type=str)
@click.option('--output-file', '-o', type=click.Path(), default='training_results.yaml')
def evaluate(data_config, net_config, train_config, pretrained_path, results_dir, output_file):

    data_info = read_yaml(data_config)
    net_info = read_yaml(net_config)
    train_info = read_yaml(train_config)

    data_path = os.path.join(data_info['data_dir'], data_info['data_set_name'])

    if results_dir is None:
        results_dir = '.'
    ensure_dir(results_dir)
    
    output_file = os.path.join(results_dir, output_file)

    X_train, X_test, y_train, y_test, p_train, p_test = load_dataset(data_path)
    model_type = net_info['model_type']
    pretrained_path = net_info['pretrained_path'] if pretrained_path is None else os.path.join(pretrained_path,'model.pt')
    del net_info['model_type']
    del net_info['pretrained_path']
    net = load_model(model_type, pretrained_path=pretrained_path, device=train_info['device'], **net_info)

    for i, (name, data, labels, pars) in enumerate(zip(['train', 'test'], [X_train, X_test],[y_train, y_test],[p_train, p_test])):
        losses, embeddings = run_epoch(data, labels, pars,
                                   net, library, 0, None,
                                   train=False,
                                   batch_size=train_info['batch_size'],
                                   beta=train_info['beta'],
                                   fp_normalize=train_info['fp_normalize'],
                                   device=train_info['device'],
                                   return_embeddings=True)

         
        np.save(os.path.join(results_dir,f'embeddings_{name}.npy'), embeddings.detach().cpu().numpy())

        loss_dict = {f'{name}_total_loss': str(np.mean(losses[0])), f'{name}_recon_loss': str(np.mean(losses[1])), f'{name}_sparsity_loss': str(np.mean(losses[2])), f'{name}_parameter_loss': str(np.mean(losses[3]))}

        yaml_fn = write_yaml if i == 0 else update_yaml
        for (key, value) in loss_dict.items():
            print(key + f': {value}')
        yaml_fn(output_file, loss_dict)

@cli.command(name='classify', help="Trains a logistic regression classifier on labeled embeddings.")
@click.argument('data-path', type=str)
@click.option('--feature-name', type=str, default='embeddings')
@click.option('--classifier', type=click.Choice(['logistic_regressor', 'k_means']), default='logistic_regressor')
@click.option('--results-dir', type=str)
@click.option('--penalty', type=str, default='l2')
@click.option('--num-c', type=int, default=11)
@click.option('--k', type=int, default=10)
@click.option('--multi-class', type=str, default='ovr')
@click.option('--verbose', type=int, default=0)
@click.option('--seed', type=int, default=0)
@click.option('--output-file', '-o', type=click.Path(), default='classifier_results.yaml')
def classify(data_path, feature_name, classifier, results_dir, penalty, num_c, k, multi_class, verbose, seed, output_file):

    output_file = os.path.join(results_dir, output_file)

    X_train, X_test, y_train, y_test, p_train, p_test = load_dataset(data_path)

    z_train = np.load(os.path.join(results_dir, f'{feature_name}_train.npy'))
    z_test = np.load(os.path.join(results_dir, f'{feature_name}_test.npy'))

    Cs = np.logspace(-5, 5, num_c)
    kf = KFold(n_splits=int(len(y_train) / float(k)))

    clf_params = {
        'cv': kf,
        'random_state': seed,
        'dual': False,
        'solver': 'lbfgs',
        'class_weight': 'balanced',
        'multi_class': multi_class,
        'refit': True,
        'scoring': 'accuracy',
        'tol': 1e-2,
        'max_iter': 5000,
        'verbose': verbose}

    # Load and train classifier
    if penalty != 'none':
        clf_params.update({
            'Cs': Cs,
            'penalty': penalty
        })

    if classifier == 'logistic_regressor':
        clf = LogisticRegressionCV(**clf_params).fit(z_train, y_train)
    elif clasisfier == 'k_means':
        num_classes = len(np.unique(y_train))
        clf = KMeans(n_clusters=num_classes, random_state=seed).fit(z_train)

    y_pred = clf.predict(z_test)
    report = classification_report(y_test, y_pred, output_dict=True)
    for (key, value) in report.items():
            print(key + f': {value}')
    write_yaml(output_file, report)

@cli.command(name="generate-net-config", help="Generates a configuration file that holds editable options for a deep net.")
@click.option('--net-class', type=str, default='CNNwFC_exp_emb')
@click.option('--latent-dim', type=int, default=100)
@click.option('--in-shape', type=list, default=[2,64,64])
@click.option('--num-conv-layers', type=int, default=3)
@click.option('--kernel-sizes', type=list, default=3*[3])
@click.option('--kernel-features', type=list, default=3*[128])
@click.option('--strides', type=list, default=3*[2])
@click.option('--pooling-sizes', type=list, default=[])
@click.option('--min-dims', '-mi', type=list, default=[-1.,-1.])
@click.option('--max-dims', '-ma', type=list, default=[1.,1.])
@click.option('--num-fc-hid-layers', type=int, default=2)
@click.option('--fc-hid-dims', type=list, default=2*[128])
@click.option('--poly_order', type=int, default=3)
@click.option('--batch-norm', is_flag=True, default=True)
@click.option('--dropout', is_flag=True, default=True)
@click.option('--dropout-rate', type=float, default=.1)
@click.option('--activation-type', type=str, default='relu')
@click.option('--output-file', '-o', type=click.Path(), default='net-config.yaml')
def generate_net_config(**args):
    output_file = args['output_file']
    output_file = os.path.abspath(output_file)
    write_yaml(output_file, args)
    print(f'Successfully generated net config file at "{output_file}".')

@cli.command(name="generate-data-config", help="Generates a configuration file that holds editable options for a dataset.")
@click.option('--output-file', '-o', type=click.Path(), default='data-config.yaml')
def generate_data_config(output_file):
    output_file = os.path.abspath(output_file)
    write_yaml(output_file, get_command_defaults(generate_dataset))
    print(f'Successfully generated data config file at "{output_file}".')

@cli.command(name="generate-train-config", help="Generates a configuration file that holds editable options for training parameters.")
@click.option('--output-file', '-o', type=click.Path(), default='train-config.yaml')
def generate_train_config(output_file):
    output_file = os.path.abspath(output_file)
    write_yaml(output_file, get_command_defaults(call_train))
    print(f'Successfully generated train config file at "{output_file}".')

@cli.command(name='visualize', help='visualize the results of training')
@click.argument("data-config", type=click.Path())
@click.argument('train-config', type=click.Path())
@click.argument('fig_save_dir', type=click.Path())
@click.option('--num-samples', type=int, default=2000)
@click.option('--tt', type=str, default='test')
@click.option('--vis-res', type=int, default=10)
@click.option('--proj', type=str, default='PCA')
@click.option('--vis-time', type=int, default=50)
@click.option('--vis-alpha', type=float, default=.01)
@click.option('--visualize-latent', is_flag=True)
@click.option('--visualize-fits', is_flag=True)
@click.option('--visualize-flows', is_flag=True)
@click.option('--which-dims', type=str, multiple=True, default=[])
@click.option('--device', type=str, default='cuda')
@click.option('--seed', type=int, default=0)
def visualize(data_config, train_config, fig_save_dir, num_samples, tt,
              visualize_latent, visualize_fits, visualize_flows, which_dims, device, seed, **kwargs):
    """
    Visualize results of a single training experiment
    """
    data_info = read_yaml(data_config)
    sf = SystemFamily(**data_info)

    train_config_path = get_last_config(train_config, suffix='_train')
    if train_config_path is None:
        train_config_path = get_last_config(os.path.join(train_config, sf.data_name), suffix='_train')


    train_info = read_yaml(train_config_path)

    fig_save_dir = os.path.join(fig_save_dir, sf.data_name)

    V = Visualizer(sf, train_info, tt=tt, num_samples=num_samples, fig_save_dir=fig_save_dir, device=device, seed=seed)

    V.visualize_selected(visualize_latent, visualize_fits, visualize_flows, **kwargs)



############################################ Multiple Training Experiments #############################################

@cli.command(name='write-gridsearch-jobs', help='grid search hyperparameters')
@click.argument("scan_file", type=click.Path(exists=True))
@click.option('--save-dir', type=click.Path(), default=os.path.join(os.getcwd(), 'worker-conf'),
              help="Directory to save worker configurations")
@click.option('--cluster-type', default='local', type=click.Choice(['local', 'slurm']))
@click.option('--slurm-partition', type=str, default='main', help="Partition on which to run jobs. Only for SLURM")
@click.option('--slurm-ncpus', type=int, default=1, help="Number of CPUs per job. Only for SLURM")
@click.option('--slurm-memory', type=str, default="2GB", help="Amount of memory per job. Only for SLURM")
@click.option('--slurm-wall-time', type=str, default='6:00:00', help="Max wall time per job. Only for SLURM")
def write_gridsearch_jobs(scan_file, save_dir, cluster_type, slurm_partition, slurm_ncpus, slurm_memory, slurm_wall_time): #TODO: this was previously grid-search
    """
    Write command lines for gridsearch training experiments
    """
    worker_dicts = generate_gridsearch_worker_params(scan_file)

    if cluster_type == 'local':
        cluster_wrap = wrap_command_with_local
    elif cluster_type == 'slurm':
        cluster_wrap = partial(wrap_command_with_slurm, partition=slurm_partition, ncpus=slurm_ncpus,
                               memory=slurm_memory, wall_time=slurm_wall_time)
    else:
        raise ValueError(f'Unsupported cluster-type {cluster_type}')
    full_save_dir = os.path.join(save_dir, read_yaml(scan_file)['gs_name'])
    save_dir = ensure_dir(full_save_dir)
    write_jobs(worker_dicts, cluster_wrap, full_save_dir)
    sys.stderr.write(f'{len(worker_dicts)} jobs written to {save_dir}\n')


@cli.command(name="generate-gridsearch-config",
             help="Generates a configuration file that holds editable options for gridsearching hyperparameters.")
@click.option('--output-file', '-o', type=click.Path(), default='gridsearch-config.yaml')
def generate_gridsearch_config(output_file):
    """
    Generate gridsearch config
    """
    gs_name = 'gs_' + timestamp()
    params = {
        'scans': get_gridsearch_default_scans(),
        'parameters': get_command_defaults(call_train),
        'gs_name': gs_name
    }

    output_file = os.path.abspath(output_file)
    write_yaml(output_file, params)
    print(f'Successfully generated gridsearch config file at "{output_file}".')

#TODO: put logs  also in dir with gs name

@cli.command(name="aggregate-gridsearch-results", help="Aggregate Gridsearch results.") # TODO: is this in use?
@click.argument("results_directory", type=click.Path(exists=True))
def aggregate_gridsearch_results(results_directory):
    results = results_to_df(results_directory)
    results.to_csv(os.path.join(results_directory, 'gridsearch-aggregate-results.tsv'), sep='\t', index=False)



@cli.command(name='visualize-gridsearch', help='Visualize the results of gridsearch training experiments')
@click.argument("gs-config-path", type=click.Path())
@click.argument('fig-save-dir', type=click.Path())
@click.option('--num-samples', type=int, default=2000)
@click.option('--tt', type=str, default='test')
@click.option('--vis-res', type=int, default=10)
@click.option('--proj', type=str, default='PCA')
@click.option('--vis-time', type=int, default=50)
@click.option('--vis-alpha', type=float, default=.01)
@click.option('--visualize-latent', is_flag=True)
@click.option('--visualize-fits', is_flag=True)
@click.option('--visualize-flows', is_flag=True)
@click.option('--visualize-beta-gamma', is_flag=True)
@click.option('--device', type=str, default='cuda')
@click.option('--seed', type=int, default=0)
def visualize_gridsearch(gs_config_path, fig_save_dir, device, seed, num_samples, visualize_beta_gamma, tt, **kwargs):
    """
    Visualize gridsearch
    """
    ensure_dir(fig_save_dir) # TODO: move to visualizer? here can add a default option
    VG = VisualizerGridsearch(gs_config_path, fig_save_dir=fig_save_dir, device=device, seed=seed, num_samples=num_samples, tt=tt)
    # TODO: if visualize_beta_gamma:
    VG.visualize_across_hyperparams()
    VG.visualize_mean_per_hyperparam()
    VG.visualize_multi()


if __name__ == '__main__':
    cli()
