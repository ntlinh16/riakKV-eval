import os
import shutil
import traceback
import re
from time import sleep

from cloudal.utils import get_logger, execute_cmd, parse_config_file, getput_file, ExecuteCommandException
from cloudal.action import performing_actions_g5k
from cloudal.provisioner import g5k_provisioner
from cloudal.configurator import kubernetes_configurator, k8s_resources_configurator
from cloudal.experimenter import create_combination_dir, create_paramsweeper, define_parameters, is_job_alive, get_results

from execo_g5k import oardel
from execo_engine import slugify
from kubernetes import config
import yaml

logger = get_logger()


class CancelCombException(Exception):
    pass


class FMKe_riakkv_g5k(performing_actions_g5k):
    def __init__(self, **kwargs):
        super(FMKe_riakkv_g5k, self).__init__()
        self.args_parser.add_argument("--kube-master", dest="kube_master",
                                      help="name of kube master node",
                                      default=None,
                                      type=str)
        self.args_parser.add_argument("--setup-k8s-env", dest="setup_k8s_env",
                                      help="create namespace, setup label and volume for kube_workers for the experiment environment",
                                      action="store_true")

    def save_results(self, comb, pop_time):
        logger.info("----------------------------------")
        logger.info("6. Starting dowloading the results")

        configurator = k8s_resources_configurator()
        results_nodes = configurator.get_k8s_resources_name(resource='node',
                                                            label_selectors='service_g5k=fmke')

        comb_dir = get_results(comb=comb,
                               hosts=results_nodes,
                               remote_result_files=['/tmp/results/'],
                               local_result_dir=self.configs['exp_env']['results_dir'])

        with open(os.path.join(comb_dir, 'pop_time.txt'), 'w') as f:
            f.write(pop_time)

        logger.info("Finish dowloading the results")

    def save_results_poptime(self, comb, pop_time):
        logger.info("----------------------------------")
        logger.info("6. Starting dowloading the results")

        comb_dir = create_combination_dir(comb, result_dir=self.configs['exp_env']['results_dir'])
        with open(os.path.join(comb_dir, 'pop_time.txt'), 'w') as f:
            f.write(pop_time)
        logger.info("Finish dowloading the results")

    def deploy_fmke_client(self, kube_namespace, comb):
        t = 20 
        logger.info('-----------------------------------------------------------------')
        logger.info('Waiting %s minutes for the replication and key distribution mechanisms between DCs' % t)
        sleep(t*60)

        logger.info('-----------------------------------------------------------------')
        logger.info('5. Starting deploying fmke client to stress the riakkv database')
        fmke_client_k8s_dir = self.configs['exp_env']['fmke_yaml_path']

        logger.debug('Delete old k8s yaml files if exists')
        for filename in os.listdir(fmke_client_k8s_dir):
            if filename.startswith('create_fmke_client_') or filename.startswith('fmke_client_'):
                if '.template' not in filename:
                    try:
                        os.remove(os.path.join(fmke_client_k8s_dir, filename))
                    except OSError:
                        logger.debug("Error while deleting file")

        logger.debug('Create fmke_client folder on each fmke_client node')
        configurator = k8s_resources_configurator()
        exp_nodes = configurator.get_k8s_resources_name(resource='node',
                                                        label_selectors='service_g5k=fmke')
        cmd = 'mkdir -p /tmp/fmke_client'
        execute_cmd(cmd, exp_nodes)

        logger.debug('Create fmke_client config files to stress database for each riakkv DC')
        file_path = os.path.join(fmke_client_k8s_dir, 'fmke_client.config.template')

        test_duration = self.configs['exp_env']['test_duration']
        logger.info('test_duration = %s' % test_duration)

        logger.debug('Create the new workload ratio')
        workload = ",\n".join(["  {%s, %s}" % (key, val)
                               for key, val in self.configs['exp_env']['operations'].items()])
        operations = "{operations,[\n%s\n]}." % workload

        fmke_list = configurator.get_k8s_resources(resource='pod',
                                                   label_selectors='app=fmke',
                                                   kube_namespace=kube_namespace)
        logger.debug('Replace corresponding parameters')
        for cluster in self.configs['exp_env']['clusters']:
            fmke_IPs = list()
            for fmke in fmke_list.items:
                if cluster in fmke.metadata.name:
                    fmke_IPs.append(fmke.status.pod_ip)
            fmke_ports = [9090 for i in range(0, len(fmke_IPs))]
            # Modify fmke_client config files with new values
            with open(file_path) as f:
                doc = f.read()
                doc = doc.replace('["127.0.0.1"]', '%s' % fmke_IPs)
                doc = doc.replace("[9090]", '%s' % fmke_ports)
                doc = doc.replace("{concurrent, 16}.", "{concurrent, %s}." %
                                  comb['concurrent_clients'])
                doc = doc.replace("{duration, 3}.", "{duration, %s}." % test_duration)
                doc = doc.replace("'", '"')
                doc = re.sub(r"{operations.*", operations, doc, flags=re.S)
            file_path2 = os.path.join(fmke_client_k8s_dir, 'fmke_client_%s.config' % cluster)
            with open(file_path2, 'w') as f:
                f.write(doc)

            logger.debug(
                'Upload fmke_client config files to kube_master to be used by kubectl to run fmke_client pods')
            getput_file(hosts=exp_nodes, file_paths=[file_path2],
                        dest_location='/tmp/fmke_client/', action='put')

        logger.debug('Create create_fmke_client.yaml files to run job stress for each riakkv DC')
        file_path = os.path.join(fmke_client_k8s_dir, 'create_fmke_client.yaml.template')
        with open(file_path) as f:
            doc = yaml.safe_load(f)
        fmke_client_files = list()
        for cluster in self.configs['exp_env']['clusters']:
            doc['spec']['parallelism'] = comb['n_fmke_client_per_dc']
            doc['spec']['completions'] = comb['n_fmke_client_per_dc']
            doc['metadata']['name'] = 'fmke-client-%s' % cluster
            doc['spec']['template']['spec']['containers'][0]['lifecycle']['postStart']['exec']['command'] = [
                "cp", "/cluster_node/fmke_client_%s.config" % cluster, "/fmke_client/fmke_client.config"]
            doc['spec']['template']['spec']['nodeSelector'] = {
                'service_g5k': 'fmke', 'cluster_g5k': '%s' % cluster}
            file_path = os.path.join(fmke_client_k8s_dir, 'create_fmke_client_%s.yaml' % cluster)
            with open(file_path, 'w') as f:
                yaml.safe_dump(doc, f)
            fmke_client_files.append(file_path)

        logger.info("Running fmke client instances on each DC")
        logger.debug("Init configurator: k8s_resources_configurator")
        configurator = k8s_resources_configurator()
        configurator.deploy_k8s_resources(files=fmke_client_files, namespace=kube_namespace)

        logger.info("Stressing database in %s minutes ....." % test_duration)
        configurator.wait_k8s_resources(resource='job',
                                        label_selectors="app=fmke-client",
                                        timeout=(test_duration + 5)*60,
                                        kube_namespace=kube_namespace)
        logger.info("Finish stressing riakkv database")

    def deploy_fmke_app(self, kube_namespace, comb, riakkv_sites):
        logger.info('------------------------------------')
        logger.info('3. Starting deploying FMKe benchmark')
        fmke_k8s_dir = self.configs['exp_env']['fmke_yaml_path']

        logger.debug('Delete old deployment files')
        for filename in os.listdir(fmke_k8s_dir):
            if '.template' not in filename:
                try:
                    os.remove(os.path.join(fmke_k8s_dir, filename))
                except OSError:
                    logger.debug("Error while deleting file")

        logger.debug('Create headless service file')
        file1 = os.path.join(fmke_k8s_dir, 'headlessService.yaml.template')
        file2 = os.path.join(fmke_k8s_dir, 'headlessService.yaml')
        shutil.copyfile(file1, file2)

        logger.debug('Create FMKe statefulSet files for each DC')
        file_path = os.path.join(fmke_k8s_dir, 'statefulSet_fmke.yaml.template')
        with open(file_path) as f:
            doc = yaml.safe_load(f)
        
        for i in range(1,11):
            if 2 ** i > comb['concurrent_clients']:
                connection_pool_size = 2 ** i
                break
        logger.debug("Init configurator: k8s_resources_configurator")
        configurator = k8s_resources_configurator()
        service_list = configurator.get_k8s_resources(resource='service',
                                                      label_selectors='app=riakkv,type=exposer-service',
                                                      kube_namespace=kube_namespace)
        for cluster in self.configs['exp_env']['clusters']:
            # Get IP of riak service for each site
            for service in service_list.items:
                if cluster in service.metadata.name:
                    ip = service.spec.cluster_ip                                 
            doc['spec']['replicas'] = comb['n_fmke_app_per_dc']
            doc['metadata']['name'] = 'fmke-%s' % cluster
            doc['spec']['template']['spec']['containers'][0]['env'] = [
                {'name': 'DATABASE_ADDRESSES', 'value': ip},
                {'name': 'TARGET_DATABASE', 'value': 'riak'},
                {'name': 'CONNECTION_POOL_SIZE', 'value': '%s' % connection_pool_size}]
            doc['spec']['template']['spec']['nodeSelector'] = {'service_g5k': 'fmke', 'cluster_g5k': '%s' % cluster}
            file_path = os.path.join(fmke_k8s_dir, 'statefulSet_fmke_%s.yaml' % cluster)
            with open(file_path, 'w') as f:
                yaml.safe_dump(doc, f)

        # for cluster in self.configs['exp_env']['clusters']:
        #     ips = '", "'.join(riakkv_sites[cluster]['pod_ips'])
        #     doc['spec']['replicas'] = comb['n_fmke_app_per_dc']
        #     doc['metadata']['name'] = 'fmke-%s' % cluster
        #     doc['spec']['template']['spec']['containers'][0]['env'] = [
        #         {'name': 'DATABASE_ADDRESSES', 'value': ips},
        #         {'name': 'TARGET_DATABASE', 'value': 'riak'},
        #         {'name': 'CONNECTION_POOL_SIZE', 'value': '%s' % connection_pool_size}]
        #     doc['spec']['template']['spec']['nodeSelector'] = {'service_g5k': 'fmke', 'cluster_g5k': '%s' % cluster}
        #     file_path = os.path.join(fmke_k8s_dir, 'statefulSet_fmke_%s.yaml' % cluster)
        #     with open(file_path, 'w') as f:
        #         yaml.safe_dump(doc, f)

        logger.info("Starting FMKe instances on each riakkv site")
        configurator.deploy_k8s_resources(path=fmke_k8s_dir, namespace=kube_namespace)

        logger.info('Waiting until all fmke app servers are up')
        deploy_ok = configurator.wait_k8s_resources(resource='pod',
                                                    label_selectors="app=fmke",
                                                    timeout=600,
                                                    kube_namespace=kube_namespace)

        if not deploy_ok:
            raise CancelCombException("Cannot wait until all fmke app servers are up")
        logger.info("Checking if FMKe_app deployed correctly")
        fmke_app_list = configurator.get_k8s_resources_name(resource='pod',
                                                            label_selectors='app=fmke',
                                                            kube_namespace=kube_namespace)
        if len(fmke_app_list) != comb['n_fmke_app_per_dc'] * len(self.configs['exp_env']['clusters']):
            logger.info("n_fmke_app = %s, n_deployed_fmke_app = %s" %
                        (comb['n_fmke_app_per_dc']*len(self.configs['exp_env']['clusters']), len(fmke_app_list)))
            raise CancelCombException("Cannot deploy enough FMKe_app")

        logger.info('Finish deploying FMKe benchmark')

    def deploy_fmke_pop(self, kube_namespace, comb):
        logger.info('---------------------------')
        logger.info('4. Starting deploying FMKe populator')
        fmke_k8s_dir = self.configs['exp_env']['fmke_yaml_path']

        logger.debug('Modify the populate_data template file')
        configurator = k8s_resources_configurator()
        fmke_list = configurator.get_k8s_resources(resource='pod',
                                                   label_selectors='app=fmke',
                                                   kube_namespace=kube_namespace)
        fmke_IPs = list()
        for cluster in self.configs['exp_env']['clusters']:
            for fmke in fmke_list.items:
                if cluster in fmke.metadata.name:
                    fmke_IPs.append('fmke@%s' % fmke.status.pod_ip)
        with open(os.path.join(fmke_k8s_dir, 'populate_data.yaml.template')) as f:
            doc = yaml.safe_load(f)
        doc['metadata']['name'] = 'populate-data-without-prescriptions'
        doc['spec']['template']['spec']['containers'][0]['args'] = ['-f -d %s --noprescriptions -p %s' %
                                                                    (comb['dataset'], comb['n_fmke_pop_process'])] + fmke_IPs
        with open(os.path.join(fmke_k8s_dir, 'populate_data.yaml'), 'w') as f:
            yaml.safe_dump(doc, f)

        logger.info("Populating the FMKe benchmark data without prescriptions")
        logger.debug("Init configurator: k8s_resources_configurator")
        configurator = k8s_resources_configurator()
        configurator.deploy_k8s_resources(files=[os.path.join(fmke_k8s_dir, 'populate_data.yaml')],
                                          namespace=kube_namespace)

        logger.info('Waiting for populating data without prescriptions')
        deploy_ok = configurator.wait_k8s_resources(resource='job',
                                                    label_selectors="app=fmke_pop",
                                                    timeout=1200,
                                                    kube_namespace=kube_namespace)
        if not deploy_ok:
            raise CancelCombException("Cannot wait until finishing populating data")

        logger.info('Checking if the populating process finished successfully or not')
        fmke_pop_pods = configurator.get_k8s_resources_name(resource='pod',
                                                            label_selectors='job-name=populate-data-without-prescriptions',
                                                            kube_namespace=kube_namespace)
        logger.debug('FMKe pod name: %s' % fmke_pop_pods[0])
        pop_result = dict()
        if len(fmke_pop_pods) > 0:
            log = configurator.get_k8s_pod_log(
                pod_name=fmke_pop_pods[0], kube_namespace=kube_namespace)
            last_line = log.strip().split('\n')[-1]
            logger.info('Last line of log: %s' % last_line)
            if 'Populated' in last_line and 'entities in' in last_line and 'avg' in last_line:
                result = log.strip().split('\n')[-1].split(' ')
                if len(result) == 8:
                    pop_result = result[4] + "\n" + result[6]
                if len(result) == 9:
                    pop_result = result[4] + "\n" + result[7]
            else:
                raise CancelCombException("Populating process ERROR")
            logger.debug("FMKe populator result: \n%s" % pop_result)

        logger.debug('Modify the populate_data file to populate prescriptions')
        with open(os.path.join(fmke_k8s_dir, 'populate_data.yaml.template')) as f:
            doc = yaml.safe_load(f)
        doc['metadata']['name'] = 'populate-data-with-onlyprescriptions'
        doc['spec']['template']['spec']['containers'][0]['args'] = [
            '-f --onlyprescriptions -p 1'] + fmke_IPs
        with open(os.path.join(fmke_k8s_dir, 'populate_data.yaml'), 'w') as f:
            yaml.safe_dump(doc, f)

        logger.info("Populating the FMKe benchmark data with prescriptions")
        configurator.deploy_k8s_resources(files=[os.path.join(fmke_k8s_dir, 'populate_data.yaml')],
                                          namespace=kube_namespace)

        logger.info('Waiting for populating data without only prescriptions')
        configurator.wait_k8s_resources(resource='job',
                                        label_selectors="app=fmke_pop",
                                        timeout=2400,
                                        kube_namespace=kube_namespace)
        logger.info('Checking if the populating process finished successfully or not')
        fmke_pop_pods = configurator.get_k8s_resources_name(resource='pod',
                                                            label_selectors='job-name=populate-data-with-onlyprescriptions',
                                                            kube_namespace=kube_namespace)
        logger.debug('FMKe pod: %s' % fmke_pop_pods[0])
        if len(fmke_pop_pods) > 0:
            log = configurator.get_k8s_pod_log(
                pod_name=fmke_pop_pods[0], kube_namespace=kube_namespace)
            last_line = log.strip().split('\n')[-1]
            logger.info('Last line of log: %s' % last_line)
            if 'Populated' not in last_line:
                raise CancelCombException("Populating process ERROR")
        logger.info('Finish populating data')

        return pop_result

    def deploy_riakkv(self, kube_namespace, comb):
        logger.info('--------------------------------------')
        logger.info('2. Starting deploying riakkv cluster')
        riakkv_k8s_dir = self.configs['exp_env']['riakkv_yaml_path']

        logger.debug('Delete old configuration files if exists')
        for filename in os.listdir(riakkv_k8s_dir):
            if filename.startswith('statefulSet_') or filename.startswith('exposer-service_'):
                if '.template' not in filename:
                    try:
                        os.remove(os.path.join(riakkv_k8s_dir, filename))
                    except OSError:
                        logger.debug("Error while deleting old configuration files")

        logger.debug('Modify the statefulSet file')
        deploy_files = [os.path.join(riakkv_k8s_dir, 'headlessService.yaml')]
        file_path = os.path.join(riakkv_k8s_dir, 'statefulSet.yaml.template')
        with open(file_path) as f:
            doc = yaml.safe_load(f)
        for cluster in self.configs['exp_env']['clusters']:
            doc['spec']['replicas'] = comb['n_riakkv_per_dc']
            doc['metadata']['name'] = 'riakkv-%s' % cluster
            doc['spec']['template']['spec']['nodeSelector'] = {
                'service_g5k': 'riakkv', 'cluster_g5k': '%s' % cluster}
            file_path = os.path.join(riakkv_k8s_dir, 'statefulSet_%s.yaml' % cluster)
            with open(file_path, 'w') as f:
                yaml.safe_dump(doc, f)
            deploy_files.append(file_path)

        logger.info("Starting RiakKV instances")
        logger.debug("Init configurator: k8s_resources_configurator")
        configurator = k8s_resources_configurator()
        configurator.deploy_k8s_resources(files=deploy_files, namespace=kube_namespace)

        logger.info('Waiting until all riakkv instances are up')
        deploy_ok = configurator.wait_k8s_resources(resource='pod',
                                                    label_selectors="app=riakkv",
                                                    timeout=600,
                                                    kube_namespace=kube_namespace)
        if not deploy_ok:
            raise CancelCombException("Cannot deploy enough RiakKV instances")

        riakkv_sites = dict()
        for cluster in self.configs["exp_env"]["clusters"]:
            riakkv_sites[cluster] = dict()
            riakkv_sites[cluster]['host_names'] = list()
            riakkv_sites[cluster]['pod_names'] = list()
            riakkv_sites[cluster]['pod_ips'] = list()

        riakkv_pods = configurator.get_k8s_resources(resource="pod",
                                                     label_selectors="app=riakkv",
                                                     kube_namespace=kube_namespace)
        riak_master_ip = None
        riak_pod_names = list()
        for pod in riakkv_pods.items:
            cluster = pod.spec.node_name.split("-")[0].strip()
            if pod.spec.node_name not in riakkv_sites[cluster]['host_names']:
                riakkv_sites[cluster]['host_names'].append(pod.spec.node_name)
            riakkv_sites[cluster]['pod_names'].append(pod.metadata.name)
            riakkv_sites[cluster]['pod_ips'].append(pod.status.pod_ip)

            if not riak_master_ip:
                riak_master_ip = pod.status.pod_ip
                riak_master_name = pod.metadata.name
                continue
            riak_pod_names.append(pod.metadata.name)
        logger.debug('riakkv_sites = %s' % riakkv_sites)
        logger.debug('riakkv_master_ip = %s' % riak_master_ip)
        logger.debug('riakkv_master_name = %s' % riak_master_name)
        logger.debug('riakkv_pod_names = %s' % riak_pod_names)

        # an example of riakkv_sites:
        # riakkv_sites = {'econome':
        #                         {'pod_ips': ['10.244.4.36', '10.244.6.42', '10.244.3.43'],
        #                          'host_names': ['econome-26.nantes.grid5000.fr', 'econome-32.nantes.grid5000.fr'],
        #                          'pod_names': ['riakkv-econome-0', 'riakkv-econome-1', 'riakkv-econome-2']},
        #                'parasilo':
        #                         {'pod_ips': ['10.244.5.33', '10.244.2.51', '10.244.1.38'],
        #                          'host_names': ['parasilo-26.nantes.grid5000.fr', 'parasilo-32.nantes.grid5000.fr'],
        #                          'pod_names': ['riakkv-parasilo-0', 'riakkv-parasilo-1', 'riakkv-parasilo-2']}
        # 
        logger.info("Creating RiakKV ring(s)")
        for cluster, cluster_info in riakkv_sites.items():
            logger.info("On %s site:" % cluster)
            logger.debug("cluster_info: %s" % cluster_info)
            if len(cluster_info['pod_names']) > 0:
                logger.info("  -->Joining all RiakKV instances")
                for pod_name in cluster_info['pod_names']:
                    configurator.execute_command(pod_name=pod_name,
                                                command="riak-admin cluster join riak@%s" % cluster_info['pod_ips'][0],
                                                kube_namespace=kube_namespace)
                logger.info("  --> Check RiakKV cluster plan")
                configurator.execute_command(pod_name=cluster_info['pod_names'][0],
                                            command="riak-admin cluster plan",
                                            kube_namespace=kube_namespace)
                logger.info("  --> Commiting RiakKV cluster plan")
                configurator.execute_command(pod_name=cluster_info['pod_names'][0],
                                            command="riak-admin cluster commit",
                                            kube_namespace=kube_namespace)
                count = 10
                while True:
                    count -= 1
                    sleep(30)
                    result = configurator.execute_command(pod_name=cluster_info['pod_names'][0],
                                                        command="riak-admin cluster status",
                                                        kube_namespace=kube_namespace)
                    if "Ring ready: true" in result:
                        logger.debug("Create RiakKV ring on %s site successfully!" % cluster)
                        break
                    if count <= 0:
                        raise CancelCombException("Cannot create RiakKV ring on %s site" % cluster)
                
                logger.info("  --> Set cluster name on %s site" % cluster)
                configurator.execute_command(pod_name=cluster_info['pod_names'][0],
                                            command="riak-repl clustername %s" % cluster,
                                            kube_namespace=kube_namespace)    
            else:    
                raise CancelCombException("There is no RiakKV pod on %s site" % cluster)

        if len(self.configs['exp_env']['clusters']) > 1:
            logger.info('Creating RiakKV cluster')
            for cluster, cluster_info in riakkv_sites.items():
                configurator.execute_command(pod_name=cluster_info['pod_names'][0],
                                            command="sed -i -e s/127.0.0.1/%s/g /etc/riak/advanced.config" % cluster_info['pod_ips'][0],
                                            kube_namespace=kube_namespace) 
                configurator.execute_command(pod_name=cluster_info['pod_names'][0],
                                            command="riak restart",
                                            kube_namespace=kube_namespace)
            sleep(120)
            for cluster, cluster_info in riakkv_sites.items():
                cluster_list = [each for each,_ in riakkv_sites.items() if each != cluster]
                for other_cluster in cluster_list:
                    configurator.execute_command(pod_name=cluster_info['pod_names'][0],
                                                command="riak-repl connect %s:9080" % riakkv_sites[other_cluster]['pod_ips'][0],
                                                kube_namespace=kube_namespace)
                    configurator.execute_command(pod_name=cluster_info['pod_names'][0],
                                                command="riak-repl realtime enable %s" % other_cluster,
                                                kube_namespace=kube_namespace) 
                    configurator.execute_command(pod_name=cluster_info['pod_names'][0],
                                                command="riak-repl realtime start %s" % other_cluster,
                                                kube_namespace=kube_namespace)
        logger.info('Adding bucket-type for RiakKV to be used by FMKe benchmark')
        for cluster,_ in riakkv_sites.items():
            r = configurator.execute_command(pod_name=riakkv_sites[cluster]['pod_names'][0],
                                            command="""riak-admin bucket-type create sets {"props":{"datatype":"set"}}""",
                                            kube_namespace=kube_namespace)
            logger.debug('result = %s' % r)
            r = configurator.execute_command(pod_name=riakkv_sites[cluster]['pod_names'][0],
                                            command="riak-admin bucket-type activate sets",
                                            kube_namespace=kube_namespace)
            logger.debug('result = %s' % r)
            r = configurator.execute_command(pod_name=riakkv_sites[cluster]['pod_names'][0],
                                            command="""riak-admin bucket-type create maps {"props":{"datatype":"map"}}""",
                                            kube_namespace=kube_namespace)
            logger.debug('result = %s' % r)
            r = configurator.execute_command(pod_name=riakkv_sites[cluster]['pod_names'][0],
                                            command="riak-admin bucket-type activate maps",
                                            kube_namespace=kube_namespace)
            logger.debug('result = %s' % r)


        logger.debug('Creating exposer-service.yaml files')
        deploy_files = list()
        file_path = os.path.join(riakkv_k8s_dir, 'exposer-service.yaml.template')
        with open(file_path) as f:
            doc = yaml.safe_load(f)
        for cluster, cluster_info in riakkv_sites.items():
            doc['spec']['selector']['statefulset.kubernetes.io/pod-name'] = cluster_info['pod_names'][0]
            doc['metadata']['name'] = 'riakkv-exposer-%s' % cluster
            file_path = os.path.join(riakkv_k8s_dir, 'exposer-service_%s.yaml' % cluster)
            with open(file_path, 'w') as f:
                yaml.safe_dump(doc, f)
            deploy_files.append(file_path)
        
        logger.info("Deploying RiakKV exposing services")
        configurator.deploy_k8s_resources(files=deploy_files, namespace=kube_namespace)
        logger.info('Waiting until all exposing services are created')
        deploy_ok = configurator.wait_k8s_resources(resource='pod',
                                                    label_selectors='app=riakkv,type=exposer-service',
                                                    kube_namespace=kube_namespace)
        if not deploy_ok:
            raise CancelCombException("Cannot connect RiakKV instances to create DC")




        logger.info('Finish deploying the riakkv cluster')
        return riakkv_sites

    def clean_k8s_resources(self, kube_namespace, n_fmke_client_per_dc):
        logger.info('1. Deleting all k8s resource from the previous run in namespace "%s"' %
                    kube_namespace)
        logger.info(
            'Delete namespace "%s" to delete all the resources, then create it again' % kube_namespace)
        configurator = k8s_resources_configurator()
        configurator.delete_namespace(kube_namespace)
        configurator.create_namespace(kube_namespace)

        if n_fmke_client_per_dc > 0:
            logger.debug('Delete all files in /tmp/results folder on fmke_client nodes')
            results_nodes = configurator.get_k8s_resources_name(resource='node',
                                                                label_selectors='service_g5k=fmke',
                                                                kube_namespace=kube_namespace)
            cmd = 'rm -rf /tmp/results && mkdir -p /tmp/results'
            execute_cmd(cmd, results_nodes)

    def run_exp_workflow(self, kube_namespace, comb, kube_master, sweeper):
        comb_ok = False
        try:
            logger.info('=======================================')
            logger.info('Performing combination: ' + slugify(comb))

            self.clean_k8s_resources(kube_namespace, comb['n_fmke_client_per_dc'])
            riakkv_sites = self.deploy_riakkv(kube_namespace, comb)
            self.deploy_fmke_app(kube_namespace, comb, riakkv_sites)
            pop_result = self.deploy_fmke_pop(kube_namespace, comb)
            if comb['n_fmke_client_per_dc'] > 0:
                self.deploy_fmke_client(kube_namespace, comb)
                self.save_results(comb, pop_result)
            else:
                self.save_results_poptime(comb, pop_result)
            comb_ok = True
        except (ExecuteCommandException, CancelCombException) as e:
            comb_ok = False
        finally:
            if comb_ok:
                sweeper.done(comb)
                logger.info('Finish combination: %s' % slugify(comb))
            else:
                sweeper.cancel(comb)
                logger.warning(slugify(comb) + ' is canceled')
            logger.info('%s combinations remaining\n' % len(sweeper.get_remaining()))
        return sweeper

    def _set_kube_workers_label(self, kube_workers):
        configurator = k8s_resources_configurator()
        clusters = dict()
        for host in kube_workers:
            cluster = host.split('-')[0]
            clusters[cluster] = [host] + clusters.get(cluster, list())
            configurator.set_labels_node(nodename=host,
                                         labels='cluster_g5k=%s' % cluster)

        n_fmke_per_dc = max(max(self.normalized_parameters['n_fmke_app_per_dc']), max(self.normalized_parameters['n_fmke_client_per_dc'])) 
        n_riakkv_per_dc = max(self.normalized_parameters['n_riakkv_per_dc'])

        for cluster, list_of_hosts in clusters.items():
            for n, service_name in [(n_riakkv_per_dc, 'riakkv'), (n_fmke_per_dc, 'fmke')]:
                for host in list_of_hosts[0: n]:
                    configurator.set_labels_node(nodename=host,
                                                 labels='service_g5k=%s' % service_name)
                list_of_hosts = list_of_hosts[n:]

    def _setup_g5k_kube_volumes(self, kube_workers, n_pv=3):
        logger.info("Setting volumes on %s kubernetes workers" % len(kube_workers))
        cmd = '''umount /dev/sda5;
                 mount -t ext4 /dev/sda5 /tmp'''
        execute_cmd(cmd, kube_workers)
        logger.debug('Create n_pv partitions on the physical disk to make a PV can be shared')
        cmd = '''for i in $(seq 1 %s); do
                     mkdir -p /tmp/pv/vol${i}
                     mkdir -p /mnt/disks/vol${i}
                     mount --bind /tmp/pv/vol${i} /mnt/disks/vol${i}
                 done''' % n_pv
        execute_cmd(cmd, kube_workers)

        logger.info("Creating local persistance volumes on Kubernetes cluster")
        logger.debug("Init configurator: k8s_resources_configurator")
        configurator = k8s_resources_configurator()
        riakkv_k8s_dir = self.configs['exp_env']['riakkv_yaml_path']
        deploy_files = [os.path.join(riakkv_k8s_dir, 'local_persistentvolume.yaml'),
                        os.path.join(riakkv_k8s_dir, 'storageClass.yaml')]
        configurator.deploy_k8s_resources(files=deploy_files)

        logger.info('Waiting for setting local persistance volumes')
        configurator.wait_k8s_resources(resource='pod',
                                        label_selectors="app.kubernetes.io/instance=local-volume-provisioner")

    def _get_credential(self, kube_master):
        home = os.path.expanduser('~')
        kube_dir = os.path.join(home, '.kube')
        if not os.path.exists(kube_dir):
            os.mkdir(kube_dir)
        getput_file(hosts=[kube_master], file_paths=['~/.kube/config'],
                    dest_location=kube_dir, action='get')
        kube_config_file = os.path.join(kube_dir, 'config')
        config.load_kube_config(config_file=kube_config_file)
        logger.info('Kubernetes config file is stored at: %s' % kube_config_file)

    def deploy_k8s(self, kube_master):
        logger.debug("Init configurator: kubernetes_configurator")
        configurator = kubernetes_configurator(hosts=self.hosts, kube_master=kube_master)
        _, kube_workers = configurator.deploy_kubernetes_cluster()

        return kube_workers

    def setup_k8s_env(self, kube_master, kube_namespace, kube_workers):
        self._get_credential(kube_master)

        logger.info('Create k8s namespace "%s" for this experiment' % kube_namespace)
        configurator = k8s_resources_configurator()
        configurator.create_namespace(namespace=kube_namespace)

        self._setup_g5k_kube_volumes(kube_workers, n_pv=3)

        logger.info('Set labels for all kubernetes workers')
        self._set_kube_workers_label(kube_workers)

        logger.info("Finish deploying the Kubernetes cluster")

    def config_host(self, kube_master_site, kube_namespace):
        logger.info("Starting configuring nodes")
        kube_master = self.args.kube_master
        if kube_master is None:
            for host in self.hosts:
                if host.startswith(kube_master_site):
                    kube_master = host
                    break

        if self.args.kube_master is None:
            kube_workers = self.deploy_k8s(kube_master)
            self.setup_k8s_env(kube_master, kube_namespace, kube_workers)
        elif self.args.setup_k8s_env:
            logger.info('Kubernetes master: %s' % kube_master)
            kube_workers = [host for host in self.hosts if host != kube_master]
            self.setup_k8s_env(kube_master, kube_namespace, kube_workers)
        else:
            self._get_credential(kube_master)

        logger.info("Finish configuring nodes")
        return kube_master

    def setup_env(self, kube_master_site, kube_namespace):
        logger.info("STARTING SETTING THE EXPERIMENT ENVIRONMENT")
        logger.info("Starting provisioning nodes on G5K")
        logger.info("Init provisioner: g5k_provisioner")
        provisioner = g5k_provisioner(configs=self.configs,
                                      keep_alive=self.args.keep_alive,
                                      out_of_chart=self.args.out_of_chart,
                                      oar_job_ids=self.args.oar_job_ids,
                                      no_deploy_os=self.args.no_deploy_os,
                                      is_reservation=self.args.is_reservation,
                                      job_name="cloudal_k8s_fmke")

        provisioner.provisioning()
        self.hosts = provisioner.hosts
        oar_job_ids = provisioner.oar_result
        self.oar_result = provisioner.oar_result

        kube_master = self.config_host(kube_master_site, kube_namespace)

        self.args.oar_job_ids = None
        logger.info("FINISH SETTING THE EXPERIMENT ENVIRONMENT\n")
        return kube_master, oar_job_ids

    def create_configs(self):
        logger.debug('Get the k8s master node')
        kube_master_site = self.configs['exp_env']['kube_master_site']
        if kube_master_site is None or kube_master_site not in self.configs['exp_env']['clusters']:
            kube_master_site = self.configs['exp_env']['clusters'][0]

        n_nodes_per_cluster = (
            max(max(self.normalized_parameters['n_fmke_app_per_dc']), max(self.normalized_parameters['n_fmke_client_per_dc'])) +
            max(self.normalized_parameters['n_riakkv_per_dc'])
        )

        # set dataset and n_fmke_pop_process to default in case not provided
        if 'dataset' not in self.normalized_parameters:
            self.normalized_parameters['dataset'] = 'standard'

        if 'n_fmke_pop_process' not in self.normalized_parameters:
            self.normalized_parameters['n_fmke_pop_process'] = 100

        # create standard cluster information to make reservation on Grid'5000, this info using by G5k provisioner
        clusters = list()
        for cluster in self.configs['exp_env']['clusters']:
            if cluster == kube_master_site:
                clusters.append({'cluster': cluster, 'n_nodes': n_nodes_per_cluster + 1})
            else:
                clusters.append({'cluster': cluster, 'n_nodes': n_nodes_per_cluster})
        self.configs['clusters'] = clusters

        # copy all YAML template folders to a new one for this experiment run to avoid conflicting
        results_dir_name = (self.configs["exp_env"]["results_dir"]).split('/')[-1]
        riakkv_yaml_path = self.configs["exp_env"]["riakkv_yaml_path"]
        old_path = os.path.dirname(riakkv_yaml_path)
        new_path = old_path + "_" + results_dir_name
        if os.path.exists(new_path):
            shutil.rmtree(new_path)
        shutil.copytree(old_path, new_path)

        self.configs["exp_env"]["riakkv_yaml_path"] = new_path + "/riakkv_yaml"
        self.configs["exp_env"]["monitoring_yaml_path"] = new_path + "/monitoring_yaml"
        self.configs["exp_env"]["fmke_yaml_path"] = new_path + "/fmke_yaml"

        return kube_master_site

    def run(self):
        logger.debug('Parse and convert configs for G5K provisioner')
        self.configs = parse_config_file(self.args.config_file_path)

        logger.debug('Normalize the parameter space')
        self.normalized_parameters = define_parameters(self.configs['parameters'])

        logger.debug('Normalize the given configs')
        kube_master_site = self.create_configs()

        logger.info('''Your largest topology:
                        number of sites: %s
                        n_riakkv_per_dc: %s
                        n_fmke_app_per_dc: %s
                        n_fmke_client_per_dc: %s ''' % (
            len(self.configs['exp_env']['clusters']),
            max(self.normalized_parameters['n_riakkv_per_dc']),
            max(self.normalized_parameters['n_fmke_app_per_dc']),
            max(self.normalized_parameters['n_fmke_client_per_dc'])
        )
        )

        logger.info('Creating the combination list')
        sweeper = create_paramsweeper(result_dir=self.configs['exp_env']['results_dir'],
                                      parameters=self.normalized_parameters)

        kube_namespace = 'fmke-exp'
        oar_job_ids = None
        while len(sweeper.get_remaining()) > 0:
            if oar_job_ids is None:
                kube_master, oar_job_ids = self.setup_env(kube_master_site, kube_namespace)

            comb = sweeper.get_next()
            sweeper = self.run_exp_workflow(kube_namespace=kube_namespace,
                                            kube_master=kube_master,
                                            comb=comb,
                                            sweeper=sweeper)

            if not is_job_alive(oar_job_ids):
                oardel(oar_job_ids)
                oar_job_ids = None
        logger.info('Finish the experiment!!!')


if __name__ == "__main__":
    logger.info("Init engine in %s" % __file__)
    engine = FMKe_riakkv_g5k()

    try:
        logger.info("Start engine in %s" % __file__)
        engine.start()
    except Exception as e:
        logger.error('Program is terminated by the following exception: %s' % e, exc_info=True)
        traceback.print_exc()
    except KeyboardInterrupt:
        logger.info('Program is terminated by keyboard interrupt.')

    if not engine.args.keep_alive:
        logger.info('Deleting reservation')
        oardel(engine.oar_result)
        logger.info('Reservation deleted')
    else:
        logger.info('Reserved nodes are kept alive for inspection purpose.')
