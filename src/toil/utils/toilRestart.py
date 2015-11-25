# Copyright (C) 2015 UCSC Computational Genomics Lab
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A script to setup and run a hierarchical run of cluster jobs.
"""

from __future__ import absolute_import
import sys
from toil.lib.bioio import getBasicOptionParser
from toil.lib.bioio import parseBasicOptions

from toil.leader import mainLoop
from toil.common import setupToil
from toil.lib.bioio import setLoggingFromOptions
from toil.job import Job
from toil.version import version
import logging
logger = logging.getLogger( __name__ )

def main():
    """Restarts a toil workflow.
    """
    
    ##########################################
    #Construct the arguments.
    ##########################################  

    parser = getBasicOptionParser()

    parser.add_argument("--version", action='version', version=version)

    parser.add_argument("jobStore", type=str,
          help=("Store in which to place job management files \
          and the global accessed temporary files"
          "(If this is a file path this needs to be globally accessible "
          "by all machines running jobs).\n"
          "If the store already exists and restart is false an"
          " ExistingJobStoreException exception will be thrown."))

    options = parseBasicOptions(parser)
        
    ##########################################
    #Now run the toil construction/leader
    ##########################################  
        
    setLoggingFromOptions(options)
    options.restart = True
    with setupToil(options) as (config, batchSystem, jobStore):
        # Load the whole jobstore into memory in a batch
        logger.warning("Downloading entire JobStore")
        jobCache = {jobWrapper.jobStoreID: jobWrapper
            for jobWrapper in jobStore.jobs()}
        logger.warning("Jobs downloaded.")
        jobStore.clean(Job._loadRootJob(jobStore), jobCache=jobCache)
        mainLoop(config, batchSystem, jobStore, Job._loadRootJob(jobStore), jobCache=jobCache)
    
def _test():
    import doctest      
    return doctest.testmod()
