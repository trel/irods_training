
import os, json, uuid
from genquery import AS_LIST, AS_DICT, row_iterator
import irods_types
import warnings
from textwrap import dedent as _dedent

from bytes_unicode_mapper import( map_strings_recursively as _map_strings_recursively,
                                  to_bytes as _to_bytes,
                                  to_unicode as _to_unicode)

from compute_to_data_support import *

# ------------------------------------------------

def _get_object_size(callback, path):

    rv = callback.msiObjStat( path , 0)

    size = 0
    if  rv['status' ] and rv['code'] == 0:
        size = int(rv['arguments'][1].objSize)

    return str(size)


def _read_data_object(callback, name):

    rv = callback.msiDataObjOpen (  "objPath={0}".format(name), 0 )

    returnbuffer = None
    desc = None

    if rv['status'] and rv['code'] >= 0:
        desc = rv['arguments'][1]

    if type(desc) is int:
        size = _get_object_size (callback,name)
        rv = callback.msiDataObjRead ( desc, size, 0 )
        returnbuffer = rv ['arguments'][2]
    if returnbuffer:
        contents_as_string = bytes(returnbuffer.get_bytes()).decode('utf-8')
        return contents_as_string[:int(size)]
    return ""


# ------------------------------------------------

Metadata_Tag = "irods::compute_to_data_task"

def meta_stamp_R (arg,callback, rei): meta_stamp (callback,arg[0],task_id='dummy-task-id')

def meta_stamp (callback, object_path, task_id = "" , object_type = "-d"):
    METADATA_TAG = Metadata_Tag
    rv = callback.msiString2KeyValPair("{METADATA_TAG}={task_id}".format(**locals()),
                                       irods_types.KeyValPair())
    if rv ['status']:
        rv = callback.msiSetKeyValuePairsToObj(rv['arguments'][1], object_path, object_type )

    return rv['status']

# ------------------------------------------------

def _vet_acceptable_container_params (container_command , container_cfg, logger):
    if container_cfg["type"] != "docker" :
        logger("Choice of container technology now limited to Docker")
        return False
    acceptable_commands = [ "containers.run", "images.pull" ]
    if container_command not in acceptable_commands:
        logger("Docker API command must be one of: {0!r}" .format(acceptable_commands))
        return False
    return True

def _resolve_docker_method (cliHandle, attrNames):

    my_object = cliHandle

    if isinstance (attrNames, (str,bytes)):
        attrNames=attrNames.split('.')

    while my_object and attrNames:
        name = attrNames.pop(0)
        my_object = getattr(my_object,name,None)

    return my_object

# ------------------------------------------------

def get_first_eligible_input ( callback , input_colln, task_id, sort_key_func = None ):

    METADATA_TAG = Metadata_Tag

    # Start with a set of all data objects in the input collection
    # subtract from the set all objects with nonzero task id's set in iRODS metadata

    eligible_inputs = set(
       "{COLL_NAME}/{DATA_NAME}".format(**d) for d in \
                                 row_iterator( ["COLL_NAME","DATA_NAME"],
                                          "COLL_NAME = '{input_colln}'".format(**locals()),
                          AS_DICT,callback )
    )-set(
        "{COLL_NAME}/{DATA_NAME}".format(**d) for d in \
                                  row_iterator( ["COLL_NAME","DATA_NAME"],
                                                 "COLL_NAME = '{input_colln}' and META_DATA_ATTR_NAME = '{METADATA_TAG}' "
                                                 "and META_DATA_ATTR_VALUE != '' ".format(**locals()),
                                  AS_DICT,callback ) 
    )
 
    if sort_key_func:
        eligible_inputs = sorted ( list(eligible_inputs) , key = sort_key_func )
    else:
        eligible_inputs =  list(eligible_inputs)

    chosen_input = None

    if len(eligible_inputs) :
        chosen_input = eligible_inputs[0]
        meta_stamp(callback, chosen_input, task_id)

    return chosen_input

def container_dispatch(rule_args, callback, rei):

    ( docker_cmd,
      config_file,
      resc_for_data,
      output_locator,
      task_id          ) = rule_args

# - can change "stdout" to "serverLog" for less verbose console or when calling from a workflow

    logger = make_logger ( callback , "stdout")

    if not (resc_for_data != "" and this_host_tied_to_resc(callback, resc_for_data)) :
        logger("Input/output data objects must be located on a local resource"); return

    if not task_id: task_id = str(uuid.uuid1())  # - make a default UUID for this task

    with warnings.catch_warnings():      # - suppress warnings 
        warnings.simplefilter("ignore")  #   when loading ...
        import docker                    #     Python Docker API
    config = None
    if type(config_file) is str and config_file:
        config_json = _read_data_object (callback, config_file )
        config = _map_strings_recursively( json.loads(config_json), _to_unicode('utf8'))

    if not (config) or \
       not _vet_acceptable_container_params( docker_cmd, config["container"] , logger) :  return

    docker_args = [ config["container"]["image"],
     ]
    docker_opts = {}

    if docker_cmd == "containers.run":

        docker_opts ['detach'] = False
        client_user = get_user_name (callback, rei)

        src_colln = config["external"]["src_collection"]
        if not user_has_access( callback, rei, client_user, "write", collection_path = src_colln):
            logger("Calling user must have write access on source"); return

        dst_colln = config["external"]["dst_collection"]
        if not user_has_access( callback, rei, client_user, "own", collection_path = dst_colln):
            logger("Calling user must have owner access on destination collection"); return

        this_input = get_first_eligible_input (callback, src_colln, task_id )
        (_ , this_input_basename ) = split_irods_path( this_input ) 

        env =  config['container']['environment']

        if this_input: 
            env['INPUT_FILE_BASENAME'] = this_input_basename

            env_for_docker = {}
            for key,value in env.items():
                if value.find('%(') >= 0:
                    env_for_docker[ key ] = value % env 
                else:
                    env_for_docker[ key ] = value
            docker_opts['environment'] = env_for_docker

        # get vault paths

        if not this_input:
            logger("no input found")
        else:
            vault_paths = {}

            input_vault_info = {}
            input_leading_path = data_object_physical_path_in_vault( callback, this_input, resc_for_data, '1', input_vault_info)
            input_rel_path = ""
            if input_leading_path :
                rel_path = input_vault_info.get("vault_relative_path")
                if rel_path:
                    vault_paths['input'] = "/".join((config["internal"]["src_directory"],os.path.dirname(rel_path)))
                input_rel_path = rel_path

            docker_host_input_path = os.path.join(input_leading_path, os.path.dirname(input_rel_path))
            docker_guest_input_path = '/inputs'

            output_locator = dst_colln + "/." + task_id

            output_vault_info = {}
            output_leading_path = data_object_physical_path_in_vault( callback, output_locator, resc_for_data, '1', output_vault_info)
            output_rel_path = ""
            if output_leading_path:
                rel_path = output_vault_info.get("vault_relative_path")
                if rel_path:
                    vault_paths['output'] = "/".join((config["internal"]["dst_directory"],os.path.dirname(rel_path)))
                output_rel_path = rel_path

            docker_host_output_path = os.path.join(output_leading_path, os.path.dirname(output_rel_path))
            docker_guest_output_path = '/outputs'

            if vault_paths:
                docker_opts [ 'volumes' ]  = {}
                if vault_paths.get('input'):
                    docker_opts ['volumes'][docker_host_input_path] = { 'bind': docker_guest_input_path, 'mode': 'ro' }
                if vault_paths.get ('output'):
                    docker_opts ['volumes'][docker_host_output_path] = { 'bind': docker_guest_output_path, 'mode': 'rw' }

            # Must run as user 999 (iRODS service account) because iRODS vault uses permission 600
            docker_opts['user'] = 999
            # Remove the container on exit
            docker_opts['remove'] = True

            docker_method = _resolve_docker_method (docker.from_env(), docker_cmd  )
 
            # prepare target output directory
            task_output_colln = dst_colln + "/" + task_id
            callback.msiCollCreate (task_output_colln, "1", 0)

            # run the container
            docker_method( config['container']['image'], config['container']['command'], **docker_opts)

            # register the products
            callback.msiregister_as_admin ( task_output_colln, resc_for_data, docker_host_output_path, "collection", 0)

