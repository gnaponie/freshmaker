# -*- coding: utf-8 -*-
# Copyright (c) 2016  Red Hat, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Written by Jan Kaluza <jkaluza@redhat.com>

from freshmaker import log, conf, utils, db, models
from freshmaker.mbs import MBS
from freshmaker.pdc import PDC
from freshmaker.handlers import BaseHandler
from freshmaker.events import MBSModuleStateChangeEvent


class MBSModuleStateChangeHandler(BaseHandler):
    name = "MBSModuleStateChangeHandler"

    def can_handle(self, event):
        if isinstance(event, MBSModuleStateChangeEvent):
            return True

        return False

    def handle(self, event):
        """
        Update build state in db when module state changed in MBS and the
        build is submitted by Freshmaker (can find that build in db). If
        build state is 'ready', query PDC to get all modules that depends
        on this module, rebuild all these depending modules.
        """
        module_name = event.module
        module_stream = event.stream
        build_id = event.build_id
        build_state = event.build_state

        # update build state if the build is submitted by Freshmaker
        builds = db.session.query(models.ArtifactBuild).filter_by(build_id=build_id,
                                                                  type=models.ARTIFACT_TYPES['module']).all()
        if len(builds) > 1:
            raise RuntimeError("Found duplicate module build '%s' in db" % build_id)
        if len(builds) == 1:
            build = builds.pop()
            if build_state in [MBS.BUILD_STATES['ready'], MBS.BUILD_STATES['failed']]:
                log.info("Module build '%s' state changed in MBS, updating it in db.", build_id)
            if build_state == MBS.BUILD_STATES['ready']:
                build.state = models.BUILD_STATES['done']
            if build_state == MBS.BUILD_STATES['failed']:
                build.state = models.BUILD_STATES['failed']
            db.session.commit()

        # Rebuild depending modules when state of MBSModuleStateChangeEvent is 'ready'
        if build_state == MBS.BUILD_STATES['ready']:
            log.info("Triggering rebuild of modules depending on %s:%s "
                     "in MBS", module_name, module_stream)

            pdc = PDC(conf)
            modules = pdc.get_latest_modules(build_dep_name=module_name,
                                             build_dep_stream=module_stream,
                                             active='true')

            for mod in modules:
                name = mod['variant_name']
                version = mod['variant_version']
                if not self.allow_build(event, 'module', name, version):
                    log.info("Skip rebuild of %s:%s as it's not allowed by configured whitelist/blacklist",
                             name, version)
                    continue
                # bump module repo first
                commit_msg = "Bump to rebuild because of %s update" % module_name
                rev = utils.bump_distgit_repo('modules', name, branch=version, commit_msg=commit_msg, logger=log)
                build_id = self.build_module(name, version, rev)
                if build_id is not None:
                    self.record_build(event, name, 'module', build_id)

        return []