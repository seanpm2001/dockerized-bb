import multiprocessing
import os
import shutil
import sys
import urllib.parse as urlp

from buildbot.plugins import util
from buildbot.plugins import changes
from buildbot.plugins import schedulers
from buildbot.plugins import steps

import config
import scummsteps

max_jobs = getattr(config, 'max_jobs', None) or (multiprocessing.cpu_count() + 1)

# Lock to avoid running more than 1 build at the same time on a worker
# This lock is used for builder workers to avoid too high CPU load
# It's also used for fetcher worker to ensure that fetching will occur just before building
# because fetcher is locked all the way through the build process
# hence fetcher must have a maxCount of 1 in all cases
lock_build = util.WorkerLock("worker", maxCount = 1,
    maxCountForWorker = {
        'fetcher': 1,
        'builder': getattr(config, 'max_parallel_builds', 1),
    })

# builds contains all build trees
# ccache is the cache for compiled objects used by ccache
# src contains the source trees
# triggers is some working directory needed by triggers
# bshomes is used for various build systems (like Gradle) to avoid downloading things at each run
# pollers is used by poll modules to maintain their state
for data_dir in ["builds", "ccache", "src", "triggers", "bshomes", "pollers" ]:
    os.makedirs(os.path.join(config.data_dir, data_dir), exist_ok=True)
shutil.copyfile(os.path.join(config.configuration_dir, "ccache.conf"),
    os.path.join(config.data_dir, "ccache", "ccache.conf"))

class Build:
    __slots__ = ['name']

    def __init__(self, name):
        self.name = name

    def getChangeSource(self):
        pass

    def getGlobalSchedulers(self, platforms):
        pass

    def getGlobalBuilders(self):
        pass

    def getPerPlatformBuilders(self, platform):
        pass

class StandardBuild(Build):
    __slots__ = [
        'baseurl', 'giturl', 'branch',
        'nightly', 'enable_force',
        'description_',
        'lock_src']

    PATCHES = []

    def __init__(self, name, baseurl, branch, nightly = None, enable_force = True, giturl = None, description = None):
        super().__init__(name)
        if giturl is None:
            giturl = baseurl + ".git"
        self.baseurl = baseurl
        self.giturl = giturl
        self.branch = branch
        self.nightly = nightly
        self.enable_force = enable_force
        self.description_ = description
        # Lock used to avoid writing source code when it is read by another task
        self.lock_src = util.MasterLock("src-{0}".format(self.name), maxCount=sys.maxsize)

    @property
    def description(self):
        return self.description_ or self.name

    @description.setter
    def description(self, value):
        self.description_ = value

    def getChangeSource(self, settings):
        return changes.GitPoller(repourl=self.giturl,
            branches=[self.branch],
            workdir=os.path.join(config.data_dir, 'pollers', self.name),
            **settings)

    def getGlobalSchedulers(self, platforms):
        ret = list()
        change_filter = util.ChangeFilter(repository = [self.baseurl, self.giturl], branch = self.branch)

        # Fetch scheduler (triggered by event source)
        ret.append(schedulers.SingleBranchScheduler(name = "branch-scheduler-{0}".format(self.name),
                change_filter = change_filter,
                # Wait for 5 minutes before starting build
                treeStableTimer = 300,
                builderNames = [ "fetch-{0}".format(self.name) ]))

        # Nightly scheduler (started by time)
        # It's triggered after regular builds to take note of the last fetched source
        # Note that build is not started by trigger
        if self.nightly is not None:
            ret.append(schedulers.NightlyTriggerable(name = "nightly-scheduler-{0}".format(self.name),
                branch = self.branch,
                builderNames = [ "nightly-{0}".format(self.name) ],
                hour = self.nightly[0],
                minute = self.nightly[1],
                onlyIfChanged = True))

        # All compiling builders
        comp_builders = ["{0}-{1}".format(self.name, p.name) for p in platforms if p.canBuild(self)]

        # Global build scheduler (triggered by fetch build and nightly build)
        ret.append(schedulers.Triggerable(name = "build-scheduler-{0}".format(self.name), builderNames = comp_builders))

        # Force schedulers
        if self.enable_force:
            ret.append(schedulers.ForceScheduler(name = "force-scheduler-{0}-fetch".format(self.name),
                reason=util.StringParameter(name="reason", label="Reason:", required=True, size=80),
                builderNames = [ "fetch-{0}".format(self.name) ],
                codebases = [util.CodebaseParameter(codebase='', hide=True)],
                properties = [
                    util.BooleanParameter(name="clean", label="Clean", default=False),
                    util.BooleanParameter(name="package", label="Package", default=False),
                    ]))
            ret.append(schedulers.ForceScheduler(name = "force-scheduler-{0}-build".format(self.name),
                reason=util.StringParameter(name="reason", label="Reason:", required=True, size=80),
                builderNames = comp_builders,
                codebases = [util.CodebaseParameter(codebase='', hide=True)],
                properties = [
                    util.BooleanParameter(name="clean", label="Clean", default=False),
                    util.BooleanParameter(name="package", label="Package", default=False),
                    ]))

        return ret

    def getGlobalBuilders(self):
        ret = list()

        f = util.BuildFactory()
        f.useProgress = False
        f.addStep(steps.Git(mode = "incremental",
            workdir = ".",
            repourl = self.giturl,
            branch = self.branch,
            locks = [ self.lock_src.access("exclusive") ],
        ))
        if len(self.PATCHES):
            f.addStep(scummsteps.Patch(
                base_dir = config.configuration_dir,
                patches = self.PATCHES,
                workdir = ".",
                locks = [ self.lock_src.access("exclusive") ],
            ))
        if self.nightly is not None:
            # Trigger nightly scheduler to let it know the source stamp
            f.addStep(steps.Trigger(name="Updating source stamp", hideStepIf=(lambda r, s: r == util.SUCCESS),
                schedulerNames = [ "nightly-scheduler-{0}".format(self.name) ]))
        f.addStep(steps.Trigger(name="Building all platforms",
            schedulerNames = [ "build-scheduler-{0}".format(self.name) ],
            set_properties = {
                'got_revision': util.Property('got_revision', defaultWhenFalse=False),
                'clean': util.Property('clean', defaultWhenFalse=False),
                'package': util.Property('package', defaultWhenFalse=False)
            },
            updateSourceStamp = True,
            waitForFinish = True))

        ret.append(util.BuilderConfig(
            name = "fetch-{0}".format(self.name),
            # This is specific
            workername = 'fetcher',
            workerbuilddir = "/data/src/{0}".format(self.name),
            factory = f,
            tags = ["fetch"],
            locks = [ lock_build.access('counting') ],
        ))

        if self.nightly is not None:
            f = util.BuildFactory()
            f.addStep(steps.Trigger(name="Building all platforms",
                schedulerNames = [ "build-scheduler-{0}".format(self.name) ],
                updateSourceStamp = True,
                waitForFinish = True,
                set_properties = {
                    'got_revision': util.Property('got_revision', defaultWhenFalse=False),
                    'clean': True,
                    'package': True,
                }))

            ret.append(util.BuilderConfig(
                name = "nightly-{0}".format(self.name),
                # We use fetcher worker here as it will prevent building of other stuff like if a change had happened
                workername = 'fetcher',
                workerbuilddir = "/data/triggers/nightly-{0}".format(self.name),
                factory = f,
                tags = ["nightly"],
                locks = [ lock_build.access('counting') ]
            ))

        return ret

class ScummVMBuild(StandardBuild):
    __slots__ = [ 'data_files', 'verbose_build' ]

    PATCHES = [
    ]

    DATA_FILES = [
        "AUTHORS",
        "COPYING",
        "COPYING.LGPL",
        "COPYING.BSD",
        "COPYRIGHT",
        "NEWS.md",
        "README.md",
        "gui/themes/translations.dat",
        "gui/themes/scummclassic.zip",
        "gui/themes/scummmodern.zip",
        "gui/themes/scummremastered.zip",
        "dists/engine-data/access.dat",
        "dists/engine-data/cryomni3d.dat",
        "dists/engine-data/drascula.dat",
        "dists/engine-data/fonts.dat",
        "dists/engine-data/hugo.dat",
        "dists/engine-data/kyra.dat",
        "dists/engine-data/lure.dat",
        "dists/engine-data/mort.dat",
        "dists/engine-data/neverhood.dat",
        "dists/engine-data/queen.tbl",
        "dists/engine-data/sky.cpt",
        "dists/engine-data/supernova.dat",
        "dists/engine-data/teenagent.dat",
        "dists/engine-data/titanic.dat",
        "dists/engine-data/tony.dat",
        "dists/engine-data/toon.dat",
        "dists/engine-data/ultima.dat",
        "dists/engine-data/wintermute.zip",
        "dists/engine-data/xeen.ccs",
        "dists/networking/wwwroot.zip",
        "dists/pred.dic",
        # Not in stable
        "dists/engine-data/cryo.dat",
        "dists/engine-data/macgui.dat",
        "dists/engine-data/macventure.dat",
        "dists/engine-data/myst3.dat",
        "dists/engine-data/grim-patch.lab",
        "dists/engine-data/monkey4-patch.m4b"
    ]

    def __init__(self, *args, **kwargs):
        verbose_build = kwargs.pop('verbose_build', False)
        data_files = kwargs.pop('data_files', None)

        super().__init__(*args, **kwargs)
        self.verbose_build = verbose_build
        if data_files is None:
            data_files = self.DATA_FILES
        self.data_files = data_files

    def getPerPlatformBuilders(self, platform):
        if not platform.canBuild(self):
            return []

        # Don't use os.path.join as builder is a linux image
        src_path = "{0}/src/{1}".format("/data", self.name)
        configure_path = src_path + "/configure"
        build_path = "{0}/builds/{1}/{2}".format("/data", platform.name, self.name)

        # snapshots_path is used in Package step on master side
        snapshots_path = os.path.join(config.snapshots_dir, self.name)
        # Ensure last path component doesn't get removed here and in packaging step
        snapshots_url = urlp.urljoin(config.snapshots_url + '/', self.name + '/')

        env = platform.getEnv(self)

        f = util.BuildFactory()
        f.useProgress = False

        f.addStep(scummsteps.Clean(
            dir = "",
            doStepIf = util.Property("clean", False)
        ))

        f.addStep(scummsteps.SetPropertyIfOlder(
            name = "check config.mk freshness",
            src = configure_path,
            generated = "config.mk",
            property = "do_configure"
            ))

        if self.verbose_build:
            platform_build_verbosity = "--enable-verbose-build"
        else:
            platform_build_verbosity = ""

        f.addStep(steps.Configure(command = [
                configure_path,
                "--enable-all-engines",
                "--disable-engine=testbed",
                platform_build_verbosity
            ] + platform.getConfigureArgs(self),
            doStepIf = util.Property("do_configure", default=True, defaultWhenFalse=False),
            env = env))

        f.addStep(steps.Compile(command = [
                "make",
                "-j{0}".format(max_jobs)
            ],
            env = env))

        if platform.canBuildTests(self):
            if platform.canRunTests(self):
                f.addStep(steps.Test(env = env))
            else:
                # Compile Tests (Runner), but do not execute (as binary is non-native)
                f.addStep(steps.Test(command = [
                        "make",
                        "test/runner" ],
                    env = env))

        packaging_cmd = None
        if platform.getPackagingCmd(self) is not None:
            packaging_cmd = platform.getPackagingCmd(self)
        else:
            if platform.getStripCmd(self) is not None:
                f.addStep(scummsteps.Strip(command = platform.getStripCmd(self),
                    env = env))

        if platform.canPackage(self):
            f.addSteps(scummsteps.get_package_steps(
                buildname = self.name,
                platformname = platform.name,
                srcpath = src_path,
                dstpath = snapshots_path,
                dsturl = snapshots_url,
                archive_format = platform.archiveext,
                disttarget = packaging_cmd,
                build_data_files = self.data_files,
                platform_data_files = platform.getDataFiles(self),
                platform_built_files = platform.getBuiltFiles(self),
                env = env))

        return [util.BuilderConfig(
            name = "{0}-{1}".format(self.name, platform.name),
            workername = 'builder',
            workerbuilddir = build_path,
            factory = f,
            locks = [ lock_build.access('counting'), self.lock_src.access("counting") ],
            tags = [self.name],
            properties = {
                "platformname": platform.name,
                "workerimage": platform.getWorkerImage(self),
            },
        )]

class ScummVMStableBuild(ScummVMBuild):
    PATCHES = [
    ]

    DATA_FILES = [
        "AUTHORS",
        "COPYING",
        "COPYING.LGPL",
        "COPYING.BSD",
        "COPYRIGHT",
        "NEWS.md",
        "README.md",
        "gui/themes/translations.dat",
        "gui/themes/scummclassic.zip",
        "gui/themes/scummmodern.zip",
        "gui/themes/scummremastered.zip",
        "dists/engine-data/access.dat",
        "dists/engine-data/cryomni3d.dat",
        "dists/engine-data/drascula.dat",
        "dists/engine-data/fonts.dat",
        "dists/engine-data/hugo.dat",
        "dists/engine-data/kyra.dat",
        "dists/engine-data/lure.dat",
        "dists/engine-data/mort.dat",
        "dists/engine-data/neverhood.dat",
        "dists/engine-data/queen.tbl",
        "dists/engine-data/sky.cpt",
        "dists/engine-data/supernova.dat",
        "dists/engine-data/teenagent.dat",
        "dists/engine-data/titanic.dat",
        "dists/engine-data/tony.dat",
        "dists/engine-data/toon.dat",
        "dists/engine-data/ultima.dat",
        "dists/engine-data/wintermute.zip",
        "dists/engine-data/xeen.ccs",
        "dists/networking/wwwroot.zip",
        "dists/pred.dic"
    ]

class ScummVMToolsBuild(StandardBuild):
    __slots__ = [ 'data_files', 'verbose_build' ]

    PATCHES = [
    ]

    DATA_FILES = [
        "COPYING",
        "NEWS",
        "README",
        "convert_dxa.sh",
        "convert_dxa.bat"
    ]

    def __init__(self, *args, **kwargs):
        verbose_build = kwargs.pop('verbose_build', False)
        data_files = kwargs.pop('data_files', None)

        super().__init__(*args, **kwargs)
        self.verbose_build = verbose_build
        if data_files is None:
            data_files = self.DATA_FILES
        self.data_files = data_files

    def getPerPlatformBuilders(self, platform):
        if not platform.canBuild(self):
            return []

        # Don't use os.path.join as builder is a linux image
        # /data is specific to builder worker
        src_path = "{0}/src/{1}".format("/data", self.name)
        configure_path = src_path + "/configure"
        build_path = "{0}/builds/{1}/{2}".format("/data", platform.name, self.name)

        # snapshots_path is used in Package step on master side
        snapshots_path = os.path.join(config.snapshots_dir, self.name)
        # Ensure last path component doesn't get removed here and in packaging step
        snapshots_url = urlp.urljoin(config.snapshots_url + '/', self.name + '/')

        env = platform.getEnv(self)

        f = util.BuildFactory()
        f.useProgress = False

        f.addStep(scummsteps.Clean(
            dir = "",
            doStepIf = util.Property("clean", False)
        ))

        f.addStep(scummsteps.SetPropertyIfOlder(
            name = "check config.mk freshness",
            src = configure_path,
            generated = "config.mk",
            property = "do_configure"
            ))

        if self.verbose_build:
            platform_build_verbosity = "--enable-verbose-build"
        else:
            platform_build_verbosity = ""

        f.addStep(steps.Configure(command = [
                configure_path,
                platform_build_verbosity
            ] + platform.getConfigureArgs(self),
            doStepIf = util.Property("do_configure", default=True, defaultWhenFalse=False),
            env = env))

        f.addStep(steps.Compile(command = [
                "make",
                "-j{0}".format(max_jobs)
            ],
            env = env))

        # No tests

        packaging_cmd = None
        if platform.getPackagingCmd(self) is not None:
            packaging_cmd = platform.getPackagingCmd(self)
        else:
            if platform.getStripCmd(self) is not None:
                f.addStep(scummsteps.Strip(command = platform.getStripCmd(self),
                    env = env))

        if platform.canPackage(self):
            f.addSteps(scummsteps.get_package_steps(
                buildname = self.name,
                platformname = platform.name,
                srcpath = src_path,
                dstpath = snapshots_path,
                dsturl = snapshots_url,
                archive_format = platform.archiveext,
                disttarget = packaging_cmd,
                build_data_files = self.data_files,
                platform_data_files = platform.getDataFiles(self),
                platform_built_files = platform.getBuiltFiles(self),
                env = env))

        return [util.BuilderConfig(
            name = "{0}-{1}".format(self.name, platform.name),
            workername = 'builder',
            workerbuilddir = build_path,
            factory = f,
            locks = [ lock_build.access('counting'), self.lock_src.access("counting") ],
            tags = [self.name],
            properties = {
                "platformname": platform.name,
                "workerimage": platform.getWorkerImage(self),
            },
        )]

builds = []

builds.append(ScummVMBuild("master", "https://github.com/scummvm/scummvm", "master", verbose_build=True, nightly=(4, 1), description="ScummVM latest"))
builds.append(ScummVMStableBuild("stable", "https://github.com/scummvm/scummvm", "branch-2-2", verbose_build=True, nightly=(4, 1), description="ScummVM stable"))
#builds.append(ScummVMBuild("gsoc2012", "https://github.com/digitall/scummvm", "gsoc2012-scalers-cont", verbose_build=True))
builds.append(ScummVMToolsBuild("tools-master", "https://github.com/scummvm/scummvm-tools", "master", verbose_build=True, nightly=(4, 1), description="ScummVM tools"))
