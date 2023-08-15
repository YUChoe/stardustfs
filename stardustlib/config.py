import json
import os

from .log import log

config = {}
default_config_filename = 'setup.json'

if os.path.isfile(default_config_filename):
    with open(default_config_filename) as fp:
        config = json.load(fp)

# TODO: config from argv

log("---- config ---")
log(config)
log("==== config ===")
