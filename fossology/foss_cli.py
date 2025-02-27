__doc__ = """
        The foss_cli cmdline interface uses the provided REST-API to communicate
        with the Fossology Server.
        Logging is implemented using the standard python logging framework.
        Log Level can be adapted using the -v/-vv option to foss_cli.
        Logging could be sent to console (option --log_to_file) and/or to a log_file
        (option --log_to_file). The name of the log_file (default is .foss_cli.log)
        could be adapted using the option --log_file_name <filename>.
"""
import pprint
import logging
from getpass import getpass
import os
import secrets
import pathlib
import sys
from logging.handlers import RotatingFileHandler
import configparser

import click

from fossology import Fossology, fossology_token
from fossology.exceptions import (
    AuthenticationError,
    FossologyApiError,
    FossologyUnsupported,
)
from fossology.obj import AccessLevel, ReportFormat, Folder, TokenScope

logger = logging.getLogger(__name__)
formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
)

FOSS_LOGGING_MAP = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}
MAX_SIZE_OF_LOGFILE = 200000
MAX_NUMBER_OF_LOGFILES = 5

DEFAULT_LOG_FILE_NAME = ".foss_cli.log"
DEFAULT_RESULT_DIR = ".foss_cli_results"
DEFAULT_CONFIG_FILE_NAME = ".foss_cli.ini"

JOB_SPEC = {
    "analysis": {
        "bucket": True,
        "copyright_email_author": True,
        "ecc": True,
        "keyword": True,
        "monk": True,
        "mime": True,
        "monk": True,
        "nomos": True,
        "ojo": True,
        "package": True,
        "specific_agent": True,
    },
    "decider": {
        "nomos_monk": True,
        "bulk_reused": True,
        "new_scanner": True,
        "ojo_decider": True,
    },
    "reuse": {
        "reuse_upload": 0,
        "reuse_group": 0,
        "reuse_main": True,
        "reuse_enhanced": True,
        "reuse_report": True,
        "reuse_copyright": True,
    },
}


def check_get_folder(ctx: dict, folder_name: str):
    """[summary]

    :param ctx: [click context]
    :type ctx: [dict]
    :param folder_name: [name of the folder]
    :type folder_name: [str]
    :raises FossologyUnsupported: [if folder_name is (not or multiple times)  found.]
    :return: [A Folder]
    :rtype: [Folder]
    """
    folder_to_use = None
    foss = ctx.obj["FOSS"]
    if folder_name == "":
        logger.warning(
            "folder_name not specified - upload will be to the Fossology Root folder."
        )
        folder_to_use = foss.rootFolder
    else:
        for a_folder in foss.folders:
            if a_folder.name == folder_name:
                logger.debug(f"Found upload folder {folder_name} with id {a_folder.id}")
                if folder_to_use is None:
                    folder_to_use = a_folder
                else:
                    description = "Multiple Folders with same name are not supported."
                    raise FossologyUnsupported(description)
        if folder_to_use is None:
            description = f"Requested Upload Folder {folder_name} does not exist."
            raise FossologyUnsupported(description)
    assert isinstance(folder_to_use, Folder)
    return folder_to_use


def check_get_report_format(format: str):
    """[summary.]

    :param format: [name of a report format]
    :type format: str
    :return: [report format]
    :rtype: [ReportFormat Attribute]
    """
    if format == "dep5":
        the_report_format = ReportFormat.DEP5
    elif format == "spx2":
        the_report_format = ReportFormat.SPDX2
    elif format == "spx2tv":
        the_report_format = ReportFormat.SPDX2TV
    elif format == "readmeoss":
        the_report_format = ReportFormat.READMEOSS
    elif format == "unifiedreport":
        the_report_format = ReportFormat.UNIFIEDREPORT
    else:
        logger.fatal(f"Impossible report format {format}")
        sys.exit(1)
    return the_report_format


def check_get_access_level(level: str):
    """[summary.]

    :param level: [name of a level]
    :type:  str
    :return: [access_level]
    :rtype: [AccessLevel Attribute]

    """
    if level == "private":
        access_level = AccessLevel.PRIVATE
    elif level == "protected":
        access_level = AccessLevel.PROTECTED
    elif level == "public":
        access_level = AccessLevel.PUBLIC
    else:
        logger.fatal(f"Impossible access level {level}")
        sys.exit(1)
    return access_level


def needs_later_initialision_of_foss_instance(ctx):
    """[summary]

    :return: [Indicates if it is a invocation needing later initialisation of foss instance]
    :rtype: [bool]
    """
    return_val = True
    logger.debug(
        f" function needs_later_initialision_of_foss_instance  Called {pprint.pformat(ctx.obj)}"
    )
    if ctx.obj["IS_REQUEST_FOR_HELP"]:
        logger.debug("Skip Initialisation as it is a --help call")
        return_val = False
    if ctx.obj["IS_REQUEST_FOR_CONFIG"]:
        logger.debug("Skip Initialisation as it is a  config call")
        return_val = False

    logger.debug(f"needs_later_initialision_of_foss_instance returne {return_val}")
    return return_val


def get_newest_upload_of_file(ctx: dict, filename: str, folder_name: str):
    """[summary]

    :param ctx: [click Context]
    :param filename: [str]
    :param folder_name: [str]
    :return: Upload Instance or None
    """

    foss = ctx.obj["FOSS"]
    the_uploads, pages = foss.list_uploads(
        folder=folder_name if folder_name else foss.rootFolder,
    )
    found = None
    newest_date = "0000-09-05 13:25:38.079869+00"
    for a_upload in the_uploads:
        if filename.endswith(a_upload.uploadname):
            if a_upload.uploaddate > newest_date:
                newest_date = a_upload.uploaddate
                found = a_upload
    if found:
        the_upload = foss.detail_upload(a_upload.id)
        logger.info(
            f"Can reuse upload for {a_upload.uploadname}. The uploads id is {a_upload.id}."
        )
        assert a_upload.id == the_upload.id
        return the_upload
    else:
        return None


def init_foss(ctx: dict):
    """[summary.]

    :param ctx: [context provided by all click-commands]
    :type ctx: [dict]
    :raises e: [Bearer TOKEN not set in environment]
    :raises e1: [Authentication with new API failed. Tried with old_api - but username was missing in environment.]
    :raises e2: [Authentication with old APi failed -too.]
    :return: [foss_instance]
    :rtype: [Fossology]
    """
    logger.debug("INIT FOSS")
    if os.path.exists(DEFAULT_CONFIG_FILE_NAME):
        config = configparser.ConfigParser()
        ctx.obj["CONFIG"] = config
        config.read(DEFAULT_CONFIG_FILE_NAME)
        assert "FOSSOLOGY" in config.sections()
        ctx.obj["TOKEN"] = config["FOSSOLOGY"]["token"]
        ctx.obj["SERVER"] = config["FOSSOLOGY"]["server_url"]
        logger.debug(
            f'set server:token from configfile  {ctx.obj["SERVER"]}:{ctx.obj["TOKEN"]} '
        )
    else:
        logger.debug("INIT FOSS: No config file found")

    if ctx.obj["TOKEN"] is None:
        try:
            ctx.obj["TOKEN"] = os.environ["FOSS_TOKEN"]
        except KeyError as e:
            logger.fatal(
                "No Token provided. Either provide FOSS_TOKEN in environment or use the -t option."
            )
            raise e
    try:
        foss = Fossology(ctx.obj["SERVER"], ctx.obj["TOKEN"])  # using new API
        ctx.obj["FOSS"] = foss
    except AuthenticationError as e1:  # Maybe it is an old version needing the username ?
        try:
            if ctx.obj["USERNAME"] is None:
                logger.fatal(
                    "Connecting to the Fossology Server using new API failed - \
                     to check with the old API a username is needed - but not provided",
                    exc_info=True,
                )
                raise e1
            else:
                foss = Fossology(
                    ctx.obj["SERVER"], ctx.obj["TOKEN"], ctx.obj["USERNAME"],
                )
        except AuthenticationError as e2:
            logger.fatal(
                'Connecting to the Fossology Server using new API failed - \
                even connecting to the old API with user {ctx.obj["USERNAME"]} failed',
                exc_info=True,
            )
            raise e2
    ctx.obj["FOSS"] = foss
    # ctx.obj["USER"] = foss.user.name
    # logger.debug(f"Logged in as user {foss.user.name}")

    return ctx.obj["FOSS"]


@click.group()
@click.option("--token", "-t", help="token to be used.")
@click.option(
    "--verbose",
    "-v",
    count=True,
    help="Increase verbosity level (e.g. -v -vv). Default is 0.",
)
@click.option(
    "--log_to_console/--no_log_to_console",
    is_flag=True,
    default=True,  # print logging events >= WARNING by default on console
    help="Send logging output to console. Default is --log_to_console.",
)
@click.option(
    "--log_to_file/--no_log_to_file",
    is_flag=True,
    default=False,
    help="Send logging output to File. Default is --no_log_to_file.",
)
@click.option(
    "--log_file_name",
    default=DEFAULT_LOG_FILE_NAME,
    help=f"Specify log File Name if log is sent to file.  Default is {DEFAULT_LOG_FILE_NAME}.",
)
@click.option(
    "--debug/--no_debug",
    is_flag=True,
    default=False,
    help="Send detailed logging output to console. Default is --nodebug.",
)
@click.option(
    "--result_dir",
    default=DEFAULT_RESULT_DIR,
    help=f"Name of the directory where foss_cli writes results. Default is {DEFAULT_RESULT_DIR}",
)
@click.pass_context
def cli(
    ctx: dict,
    token: str,
    verbose: int,
    log_to_console: bool,
    log_to_file: bool,
    log_file_name: str,
    debug: bool,
    result_dir: str,
):
    """The foss_cli cmdline.  Multiple -v increase verbosity-level.
    """
    if log_to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    if not os.path.isdir(result_dir):
        os.mkdir(result_dir)

    if log_to_file:
        logfile_handler = RotatingFileHandler(
            os.path.join(result_dir, log_file_name),
            maxBytes=MAX_SIZE_OF_LOGFILE,
            backupCount=MAX_NUMBER_OF_LOGFILES,
        )
        logfile_handler.setFormatter(formatter)
        logger.addHandler(logfile_handler)
    logger.setLevel(FOSS_LOGGING_MAP.get(verbose, logging.DEBUG))
    assert os.path.isdir(result_dir)
    ctx.obj["VERBOSE"] = verbose
    ctx.obj["TOKEN"] = token
    ctx.obj["DEBUG"] = debug
    ctx.obj["RESULT_DIR"] = result_dir

    if ctx.obj["VERBOSE"] >= 2:
        logger.debug(f"foss_cli called with: {pprint.pformat(sys.argv)}")

    foss_needs_inititialisation = needs_later_initialision_of_foss_instance(ctx)
    logger.debug(f"foss_needs_inititialisation is {foss_needs_inititialisation}")
    if needs_later_initialision_of_foss_instance(ctx):
        foss = init_foss(ctx)  # leaves the foss instance within the ctx dict
    else:
        logger.debug("No need to init foss")

    if debug:
        logger.debug("Started in debug mode")
        if foss_needs_inititialisation:
            logger.debug(f"Servers users:{pprint.pformat(foss.users)}")
            folder_ids = [folder.id for folder in foss.folders]
            for id in folder_ids:
                detail = foss.detail_folder(id)
                logger.debug(f"Get Folder {detail.id}")
                logger.debug(f"    Name: {detail.name}  Parent:   {detail.parent}")
                logger.debug(
                    f"    desc: {detail.description}  Add-Info: {detail.additional_info}"
                )
            logger.debug(f"Servers api:{pprint.pformat(foss.api)}")
            logger.debug(f"Servers version:{pprint.pformat(foss.version)}")
            logger.debug(f"User Authorized on Server:{pprint.pformat(foss.user.name)}")
            logger.debug(
                f"Root Folder on Server:{pprint.pformat(foss.rootFolder.name)}"
            )
        else:
            logger.debug("foss not initialized")


@cli.command("config")
@click.option("--server", default="http://fossology/repo", help="url of server.")
@click.option("--username", default="fossy", help="Username on Fossology Server.")
@click.option(
    "--password", default="fossy", help="Password for User on Fossology Server."
)
@click.option(
    "--token_scope",
    default="read",
    help="Access scope on Fossology Server [read/write].",
)
@click.option(
    "--interactive/--nointeractive",
    is_flag=True,
    default=True,
    help="Get Config Values via stdin. Default is --interactive.",
)
@click.pass_context
def config(
    ctx: dict,
    server: str,
    username: str,
    password: str,
    token_scope: str,
    interactive: bool,
):
    """Create a foss_cli config file."""

    if interactive:
        print("server url within the testenvironment is http://fossology/repo")
        server = input("server url: ")
        print("username/password. (within the testenvironment this is fossy/fossy)")
        username = input("username: ")
        password = getpass()
        token_scope = "undefined"
        while True:
            try:
                print("token scope. (Either read or write):")
                token_scope = input("token_scope: ")
                assert token_scope in ["read", "write"]
                break
            except Exception:
                print("Allowed values are read or write")

    logger.warning(
        f"Create New Config server: {server} username: {username} scope {token_scope}"
    )

    if token_scope == "read":
        the_token_scope = TokenScope.READ
    elif token_scope == "write":
        the_token_scope = TokenScope.WRITE
    else:
        logger.fatal(f"Impossible token_scope: {token_scope}:")
        ctx.exit(1)

    logger.debug(f"Create Token for {username} on {server}")
    token = fossology_token(
        server,
        username,
        password,
        secrets.token_urlsafe(8),  # TOKEN_NAME
        the_token_scope,
    )
    logger.debug(f"Created Token {token} ")

    path_to_cfg_file = pathlib.Path.cwd() / DEFAULT_CONFIG_FILE_NAME

    if path_to_cfg_file.exists():
        logger.info(f"Found existing foss_cli config file {path_to_cfg_file} ")
        config = configparser.ConfigParser()
        config.read(path_to_cfg_file)

    else:
        logger.info(f"Not Found existing foss_cli config file {path_to_cfg_file}")
        config = configparser.ConfigParser()

    config["FOSSOLOGY"] = {
        "SERVER_URL": server,
        "USERNAME": username,
        "TOKEN": token,
    }
    with open(path_to_cfg_file, "w") as fp:
        config.write(fp)

    for section in config.sections():
        for name, value in config.items(section):
            logger.debug(f" Section : [{section}] name: {name} value:  {value}")

    logger.warning(
        f"New Config section server: {server} username: {username} scope {token_scope} generated in {path_to_cfg_file}"
    )


@cli.command("log")
@click.option(
    "--log_level", default=0, help="Set the log_level of the message [0,1,2]."
)
@click.option("--message_text", default="log message", help="Text of the log message.")
@click.pass_context
def log(ctx: dict, log_level: int, message_text: str):
    """Add a Log Message to the log.  If a log message is printed to the log depends
       on  the verbosity defined starting the foss_cli (default level 0 /-v level 1/-vv level 2).
       Beeing on global verbosity level 0 only messages of --log_level 2 will be printed.
       Beeing on global verbosity level 1  messages of --log_level 1 and 2 will be printed.
       Beeing on global verbosity level 2 messages of --log_level 0,1,2 will be printed.
       Where the log messages are printed depends on the global configuration for --log_to_console,
       --log_to_file and --log_file_name.
    """

    if log_level == 0:
        logger.debug(message_text)
    elif log_level == 1:
        logger.info(message_text)
    elif log_level == 2:
        logger.warning(message_text)
    else:
        error_text = "Impossible Log Level in Log command."
        logger.fatal(error_text)
        raise click.UsageError(error_text, ctx=ctx)


@cli.command("create_folder")
@click.argument("folder_name")
@click.option("--folder_description", help="Description of the Folder.")
@click.option("--folder_group", help="Name of the Group owning the Folder.")
@click.pass_context
def create_folder(
    ctx: dict, folder_name: str, folder_description: str, folder_group: str
):
    """The foss_cli create_folder command."""
    foss = ctx.obj["FOSS"]
    logger.debug(
        f" Try to create folder {folder_name} for group {folder_group} desc: {folder_description}"
    )
    try:
        folder = foss.create_folder(
            foss.rootFolder,
            folder_name,
            description=folder_description,
            group=folder_group,
        )
        logger.debug(
            f"Folder {folder.name} with description {folder.description} created"
        )
    except Exception as e:
        logger.fatal(f"Error creating Folder {folder_name} ", exc_info=True)
        raise e


@cli.command("create_group")
@click.argument("group_name")
@click.pass_context
def create_group(ctx: dict, group_name: str):
    """The foss_cli create_group command."""
    logger.debug(f"Try to create group {group_name}")
    foss = ctx.obj["FOSS"]
    try:
        foss.create_group(group_name)
        logger.debug(f" group {group_name} created")
    except FossologyApiError as e:
        if "Details: Group already exists.  Not added." in e.message:
            logger.debug(
                f" group {group_name} already exists. Anyway the group is available."
            )
        else:
            logger.fatal(f"Error adding group {group_name} ", exc_info=True)
            raise e


@cli.command("upload_file")
@click.argument(
    "upload_file", type=click.Path(exists=True),
)
@click.option("--folder_name", default="", help="The name of the folderto upload to.")
@click.option("--description", default="", help="The decription of the upload.")
@click.option("--access_level", default="public", help="The acces Level  the upload.")
@click.option(
    "--reuse_newest_upload/--no_reuse_newest_upload",
    is_flag=True,
    default=False,
    help="Reuse last upload if available.",
)
@click.option(
    "--summary/--no_summary",
    is_flag=True,
    default=False,
    help="Get summary of upload.",
)
@click.pass_context
def upload_file(
    ctx: dict,
    upload_file: str,
    folder_name: str,
    description: str,
    access_level: str,
    reuse_newest_upload: bool,
    summary: bool,
):
    """The foss_cli upload_file command."""

    logger.debug(f"Try to upload file {upload_file}")
    foss = ctx.obj["FOSS"]

    # check/set the requested access level
    the_access_level = check_get_access_level(access_level)

    # check/set the requested folder
    folder_to_use = check_get_folder(ctx, folder_name)

    if reuse_newest_upload:
        the_upload = get_newest_upload_of_file(ctx, upload_file, folder_name)
    else:
        the_upload = None

    if the_upload is None:
        the_upload = foss.upload_file(
            folder_to_use,
            file=upload_file,
            description=description if description else "upload via foss-cli",
            access_level=the_access_level,
        )

    ctx.obj["UPLOAD"] = the_upload

    if summary:
        summary = foss.upload_summary(the_upload)
        if ctx.obj["DEBUG"]:
            logger.debug(
                f"Summary of Upload id {summary.id} Name {summary.uploadName} "
            )
            logger.debug(
                f"    Main License {summary.mainLicense} Unique License  {summary.uniqueLicenses} "
            )
            logger.debug(
                f"    Total License {summary.totalLicenses} Unique Concluded License  {summary.uniqueConcludedLicenses}"
            )
            logger.debug(
                f"    totalConcludedLicenses {summary.totalConcludedLicenses} FileToBeCleared  {summary.filesToBeCleared} "
            )
            logger.debug(
                f"    Files Cleared {summary.filesCleared}  ClearingStatus  {summary.clearingStatus} "
            )
            logger.debug(
                f"    CopyRightCount {summary.copyrightCount}  Add Info  {summary.additional_info} "
            )


@cli.command("schedule_jobs")
@click.argument(
    "file_name", type=click.Path(exists=True),
)
@click.option("--folder_name", default="", help="The name of the folder to upload to.")
@click.option(
    "--file_description",
    default="upload via foss-cli",
    help="The description of the upload.",
)
@click.option(
    "--dry_run/--no_dry_run",
    is_flag=True,
    default=False,
    help="Do not upload but show what would be done. Use -vv to see output.",
)
@click.option(
    "--reuse_newest_upload/--no_reuse_newest_upload",
    is_flag=True,
    default=False,
    help="Reuse newest upload if available.",
)
@click.option(
    "--reuse_newest_job/--no_reuse_newest_job",
    is_flag=True,
    default=False,
    help="Reuse newest scheduled job for the upload if available.",
)
@click.option(
    "--report_format",
    default="unifiedreport",
    help="The name of the reportformat. [dep5, spdx2,spdxtv,readmeoss,unifiedreport]",
)
@click.option(
    "--access_level",
    default="protected",
    help="The access level of the upload.[private,protected,public]",
)
@click.pass_context
def schedule_jobs(  # noqa: C901
    ctx: dict,
    file_name: str,
    file_description: str,
    folder_name: str,
    report_format: str,
    access_level: str,
    reuse_newest_upload: bool,
    reuse_newest_job: bool,
    dry_run: bool,
):
    """The foss_cli schedule_jobs command."""
    global JOB_SPEC
    logger.debug(f"Try to schedule job for {file_name}")
    foss = ctx.obj["FOSS"]

    # check/set the requested report format
    the_report_format = check_get_report_format(report_format)

    # check/set the requested access level
    the_access_level = check_get_access_level(access_level)

    # check/get the folder to use identified by the provided  folder_name
    folder_to_use = check_get_folder(ctx, folder_name)

    # check/get the foss.upload to use
    if reuse_newest_upload:
        the_upload = get_newest_upload_of_file(ctx, file_name, folder_name)
    else:
        if dry_run:
            logger.warning(
                "Skip upload as dry_run is requested without --reuse_newest_upload"
            )
            the_upload = None
        else:
            logger.debug(f"Initiate new upload for {file_name}")

            the_upload = foss.upload_file(
                folder_to_use,
                file=file_name,
                description=file_description,
                access_level=the_access_level,
            )
            logger.debug(f"Finished upload for {file_name}")

    if the_upload is None:
        logger.fatal(f"Unable to find upload for {file_name}.")
        ctx.exit(1)

    # check/get job correlated with the upload
    job = None
    if reuse_newest_job:
        logger.debug(f"Try to find a scheduled job on {the_upload.uploadname}")
        the_jobs, pages = foss.list_jobs(the_upload)
        newest_date = "0000-09-05 13:25:38.079869+00"
        for the_job in the_jobs:
            if the_job.queueDate > newest_date:
                newest_date = the_job.queueDate
                job = the_job
        if job is None:
            logger.info(f"Upload {the_upload.uplodname} never started a job ")
        else:
            logger.debug(
                f"Can reuse old job on Upload {the_upload.uploadname}: Newest Job id {job.id} is from {job.queueDate} "
            )

    if job is None:  # always true if --no_reuse_newest_job
        job = foss.schedule_jobs(
            folder_to_use if folder_to_use else foss.rootFolder,
            the_upload,
            JOB_SPEC,
            wait=True,  # we wait (default 30 sec) for the job to complete
        )
        logger.debug(f"Scheduled new job {job}")

    # check/get state of job correlated with the upload
    logger.debug(f"job  {job.id}  is in state {job.status} ")
    if job.status == "Processing":
        logger.fatal(
            f"job  {job.id}  is still in state {job.status}: Please try again later with --reuse_newest_upload --reuse_newest_job "
        )
        ctx.exit(1)

    assert job.status == "Completed"

    # trigger generation of report
    report_id = foss.generate_report(the_upload, report_format=the_report_format)
    logger.debug(f"Generated report {report_id}")

    # download report
    content, name = foss.download_report(report_id)
    logger.debug(
        f"Report downloaded: {name}:  content type: {type(content)} len:  {len(content)}."
    )

    destination_file = os.path.join(ctx.obj["RESULT_DIR"], name)
    with open(destination_file, "wb") as fp:
        written = fp.write(content)
        assert written == len(content)
        logger.info(
            f"Report written to file: report_name {name}  written to {destination_file}"
        )


def main():
    d = dict()
    d["IS_REQUEST_FOR_HELP"] = False
    d["IS_REQUEST_FOR_CONFIG"] = False
    for arg in sys.argv[1:]:
        if arg == "--help":
            d["IS_REQUEST_FOR_HELP"] = True
    for arg in sys.argv[1:]:
        if arg == "config":
            d["IS_REQUEST_FOR_CONFIG"] = True
    cli(obj=d)  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))  # pragma: no cover
