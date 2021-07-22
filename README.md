# Benchmarking the RiakKV cluster using FMKe on Grid5000 system
This experiment performs the [FMKe benchmark](https://github.com/ntlinh16/FMKe) to test the performance of an RiakKV cluster which is deployed on Grid5000 system by using an experiment management tool, [cloudal](https://github.com/ntlinh16/cloudal/).

If you do not install and configure all dependencies to use cloudal, please follow the [instruction](https://github.com/ntlinh16/cloudal)

## Introduction

The flow of this experiment follows [an experiment workflow with cloudal](https://github.com/ntlinh16/cloudal/blob/master/docs/technical_detail.md#an-experiment-workflow-with-cloudal).

The `create_combs_queue()` function creates a list of combinations from the given parameters in the _exp_setting_riakkv_fmke_g5k_ file which are (1) the number of concurrent clients connects to the database, (2) the number of iterations and (3) the topology (number of RiakKV nodes, number of FMKe_app, number of FMKe_client)

The `setup_env()` function (1) makes a reservation for the required infrastructure; and then (2) deploys a Kubernetes cluster to managed all RiakKV and FMKe services which are deployed by using Docker containers.

The `run_exp_workflow()` function performs 6 steps of a run of this experiment scenario which described detail in the following figure. With each successful run, a new directory will be created to store the results.   

## How to run the experiment

### 1. Clone the repository:

Clone the project from the git repo:

```
git clone https://github.com/ntlinh16/riakKV.git
```
### 2. Prepare configuration files:
There are two types of config files to perform this experiment.

#### Setup environment config file
You need to clarify all the following information in `exp_setting_riakkv_fmke_g5k.yaml` file:

* Infrastructure: the provided information in this part will be used to make a provisioning on nGrid5000 system. They include when and how long you want to provision nodes; the OS you want to deploy on reserved nodes.  The name of cluster and the number of nodes for each cluster you want to provision on Grid5k system will be declared in next part .

* Experiment Parameters: is a list of experiment parameters that represent different aspects of the system that you want to examine. Each parameter contains a list of possible values of that aspect. For example, I want to examine the effect of the number of concurrent clients that connect to an RiakKV database, so I define a parameter such as `concurrent_clients: [16, 32]`; and each experiment will be repeated 5 times (`iteration: [1..5]`) for a statistically significant results. And for different topologies of an RiakKV cluster I provide the number of nodes.

* Experiment environment information: the path to experiment configuration files, etc.

#### Experiment config files for Kubernetes

In this experiment, I use Kubernetes deployment files to deploy and manage RiakKV cluster, and FMKe benchmark. Therefore, you need to provide these deployment files. I already provided the template files which work well with this experiment in folder [exp_config_files](https://github.com/ntlinh16/riakKV-eval/tree/main/exp_config_files). If you do not require any special configurations, you do not have to modify these files.

### 3. Run the experiment
If you are running this experiment on your local machine, remember to run the VPN to [connect to Grid5000 system from outside](https://github.com/ntlinh16/cloudal/blob/master/docs/g5k_k8s_setting.md).

Then, run the following command:

```
cd ~/riakkv
python riakkv_fmke_g5k.py --system_config_file exp_setting_riakkv_fmke_g5k.yaml -k &>  result/test.log
```

You can watch the log by:

```
tail -f ~/riakkv/result/test.log
```
Arguments:

* `-k`: after finishing all the runs of the experiment, all provisioned nodes on Gris5000 will be kept alive so that you can connect to them, or if the experiment is interrupted in the middle, you can use these provisioned nodes to continue the experiments. This mechanism saves time since you don't have to reserve and deploy nodes again. If you do not use `-k`, when the script is finished or interrupted, all your reserved nodes will be deleted.
### 4. Re-run the experiment
If the script is interrupted by unexpected reasons. You can re-run the experiment and it will continue with the list of combinations left in the queue. You have to provide the same result directory of the previous one. There are two possible cases:

1. If your reserved hosts on G5k are dead, you just run the same above command:
```
cd ~/riakkv
python riakkv_fmke_g5k.py --system_config_file exp_setting_riakkv_fmke_g5k.yaml -k &> result/test2.log
```

2. If your reserved hosts are still alive, you can give the OAR_JOB_IDs to the script:
```
cd cloudal/examples/experiment/antidotedb_g5k/
python riakkv_fmke_g5k.py --system_config_file exp_setting_riakkv_fmke_g5k.yaml -k -j < site1:oar_job_id1,site2:oar_job_id2,...> --no-deploy-os --kube-master <the host name of the kubernetes master> &> result/test2.log
```
## Docker images used in these experiments

I use Docker images to pre-build the environment for FMKe services. All images are on Docker repository.

To deploy RiakKV cluster:

* **ntlinh/riak_kv:2.2.6_debian**

To deploy FMKe benchmark:

* **ntlinh/fmke**
* **ntlinh/fmke_pop**
* **ntlinh/fmke_client**