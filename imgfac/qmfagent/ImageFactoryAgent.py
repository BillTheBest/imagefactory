#   Copyright 2011 Red Hat, Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import logging
import uuid
import cqpid
from copy import deepcopy
from qmf2 import *
from ImageFactory import ImageFactory
from BuildAdaptor import BuildAdaptor
from imgfac import props

class ImageFactoryAgent(AgentHandler):

    qmf_object = props.prop("_qmf_object", "The qmf_object property.")
    managedObjects = props.ro_prop("_managedObjects", "The managedObjects property.")

    def __init__(self, url):
        self.log = logging.getLogger('%s.%s' % (__name__, self.__class__.__name__))
        self._managedObjects = {}
        self.session = None
        # create a connection and connect to qpidd
        # TODO: (redmine 277) - Make this use actual amqp:// urls... currently, only host works
        self.connection = cqpid.Connection(url, "{reconnect:True}")
        self.connection.open()
        # Create, configure, and open a QMFv2 agent session using the connection.
        self.session = AgentSession(self.connection)
        self.session.setVendor("redhat.com")
        self.session.setProduct("imagefactory")
        self.session.open()
        # Initialize the parent class with the session.
        AgentHandler.__init__(self, self.session)
        # Register our schemata with the agent session.
        self.session.registerSchema(ImageFactory.qmf_schema)
        self.session.registerSchema(BuildAdaptor.qmf_schema)
        self.session.registerSchema(BuildAdaptor.qmf_event_schema_status)
        self.session.registerSchema(BuildAdaptor.qmf_event_schema_percentage)
        self.session.registerSchema(BuildAdaptor.qmf_event_schema_build_failed)
        # Now add the image factory object
        self.image_factory = ImageFactory()
        self.image_factory.agent = self
        self.image_factory_addr = self.session.addData(self.image_factory.qmf_object, "image_factory")
        self.log.info("image_factory has qmf/qpid address: %s", self.image_factory_addr)

    ## AgentHandler override
    def method(self, handle, methodName, args, subtypes, addr, userId):
        """
        Handle incoming method calls.
        """
        log_args = args
        if("credentials" in args):
            log_args = deepcopy(args)
            log_args["credentials"] = "*** REDACTED ***"
        self.log.debug("Method called: name = %s \n args = %s \n handle = %s \n addr = %s \n subtypes = %s \n userId = %s", methodName, log_args, handle, addr, subtypes, userId)

        try:

            if (addr == self.image_factory_addr):
                target_obj = self.image_factory
            elif (repr(addr) in self.managedObjects):
                target_obj = self.managedObjects[repr(addr)]
            else:
                raise RuntimeError("%s does not match an object managed by ImageFactoryAgent!  Unable to respond to %s." % (repr(addr), methodName))

            result = getattr(target_obj, methodName)(**args)

            if (self._handle_image_factory_result(handle, methodName, addr, result)):
                self.session.methodSuccess(handle)
            elif(result and isinstance(result, dict)):
                for key in result:
                    handle.addReturnArgument(key, result[key])
                self.session.methodSuccess(handle)
            else:
                returned_dictionary = {}
                for method in type(target_obj).qmf_schema.getMethods():
                    if (method.getName() == methodName):
                        for method_arg in method.getArguments():
                            if (method_arg.getDirection() == DIR_OUT):
                                returned_dictionary.update({method_arg.getName() : method_arg.getDesc()})
                raise RuntimeError("Method '%s' on objects of class %s must return a dictionary of %s" % (methodName, target_obj.__class__.__name__, returned_dictionary))
        except Exception, e:
            self.log.exception(str(e))
            self.session.raiseException(handle, str(e))

    def _handle_image_factory_result(self, handle, method_name, addr, result):
        if not addr == self.image_factory_addr:
            return False

        if method_name in ("image", "provider_image"):
            handle.addReturnArgument("build_adaptor", self._add_adaptor(result, method_name))
        elif method_name in ("build_image", "push_image"):
            handle.addReturnArgument("build_adaptors", map(lambda ba: self._add_adaptor(ba, method_name), result))
        else:
            return False

        return True

    def _add_adaptor(self, build_adaptor, prefix):
        build_adaptor_instance_name = "build_adaptor:%s:%s" %  (prefix, build_adaptor.new_image_id)
        qmf_object_addr = self.session.addData(build_adaptor.qmf_object, build_adaptor_instance_name, persistent=True)
        self.managedObjects[repr(qmf_object_addr)] = build_adaptor
        return qmf_object_addr.asMap()

    def shutdown(self):
        """
        Clean up the session and connection. Cancel the running thread.
        """
        try:
            self.session.close()
            self.connection.close()
            self.cancel()
            return True
        except Exception, e:
            self.log.exception(e)
            return False

    def deregister(self, managed_object):
        """
        Remove an item from the agents collection of managed objects.
        """
        managed_object_key = None
        if(managed_object.__class__ == Data):
            managed_object_key = repr(managed_object.getAddr())
        elif(managed_object.__class__ == DataAddr):
            managed_object_key = repr(managed_object)
        elif(managed_object.__class__ == str):
            managed_object_key = managed_object

        try:
            del self.managedObjects[managed_object_key]
        except KeyError:
            self.log.error("Trying to remove object (%s) from managedObjects that does not exist..." % (managed_object_key, ))
