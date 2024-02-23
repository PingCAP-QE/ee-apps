#!/usr/bin/env python3

import argparse
import os
import urllib.request
import urllib.parse
import urllib.error
import json
import time

devbuild_url = 'https://tibuild.pingcap.net/api/devbuilds'

NOBLOCK = False
BUILD_CREATED_BY = ''
BASIC_AUTH_CREDENTIAL=''


def dev_build_url(build_id: int):
    return f"{devbuild_url}/{build_id}"


def get(id: int) -> dict:
    with urllib.request.urlopen(dev_build_url(id)) as f:
        response = f.read().decode()
        got = json.loads(response)
        return got


def trigger(args):
    data = {
        "meta":{"createdBy": BUILD_CREATED_BY},
        "spec": {
                    "edition": args.edition, "gitRef": args.gitRef, "version": args.version,
                    "product": args.product, "pluginGitRef": args.pluginGitRef,
                    "isPushGCR":args.pushGCR, "githubRepo": args.githubRepo,
                    "features": args.features, "isHotfix":args.hotfix,
                    "buildEnv": ' '.join(args.buildEnv) if args.buildEnv else '',
                    "productDockerfile": args.productDockerfile,
                    "productBaseImg": args.productBaseImg,
                    "builderImg":args.builderImg,
                    "targetImg": args.targetImg,
                    "pipelineEngine": args.engine
                }
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if BASIC_AUTH_CREDENTIAL:
        headers['Authorization']="BASIC " + BASIC_AUTH_CREDENTIAL
    body = json.dumps(data).encode()
    req = urllib.request.Request(f"{devbuild_url}?dryrun={args.dryrun}", body, headers, method="POST")
    build_id = 0
    try:
        with urllib.request.urlopen(req) as f:
            response = f.read().decode()
            created = json.loads(response)
            build_id = created['id']
            print("build id is %s" % (build_id,))
            if not args.dryrun:
                poll(build_id)
    except urllib.error.HTTPError as e:
        print("trigger error, error message:")
        print(e.read().decode())
        raise SystemExit(3)

def rerun(args):
    headers = {
        "Accept": "application/json",
    }
    req = urllib.request.Request(f"{devbuild_url}/{args.build_id}/rerun?dryrun={args.dryrun}", None, headers, method="POST")
    build_id = 0
    try:
        with urllib.request.urlopen(req) as f:
            response = f.read().decode()
            created = json.loads(response)
            build_id = created['id']
            print("build id is %s" % (build_id,))
            if not args.dryrun:
                poll(build_id)
    except urllib.error.HTTPError as e:
        print("rerun error, error message:")
        print(e.read().decode())
        raise SystemExit(3)

def poll(build_id):
    print(f"polling : {dev_build_url(build_id)}")
    printed = False
    while True:
        try:
            build = get(build_id)
        except urllib.error.HTTPError as e:
            print("get build error, error message:")
            print(e.read().decode())
            raise SystemExit(3)
        status = build['status']['status']
        if not printed and status != 'PENDING':
            print("Pipeline URL: %s" % (build['status'].get('pipelineViewURL'),))
            print("Build %d status is: %s" % (build_id, status))
            printed = True
        if status not in ('PENDING', 'PROCESSING'):
            print("Build %d finished with: %s" % (build_id, status))
            if status == "SUCCESS":
                print(get_artifact(build))
            break
        if NOBLOCK:
            break
        time.sleep(60)


def get_artifact(build: dict) -> str:
    return json.dumps(build['status'].get('buildReport', '{}'), indent=4)


if __name__ == "__main__":
    NOBLOCK = bool(os.environ.get('NOBLOCK'))
    BUILD_CREATED_BY = os.environ.get('BUILD_CREATED_BY') or ''
    BASIC_AUTH_CREDENTIAL = os.environ.get('BASIC_AUTH_CREDENTIAL') or ''
    top_parser = argparse.ArgumentParser(
        prog='tbctl',
        description='tibuild commandline client'
    )
    top_subcommands = top_parser.add_subparsers(title='subcommand')
    devbuild_parser = top_subcommands.add_parser('devbuild', aliases=["dev"],
                                                 help="dev build from pr or commit or branch")
    devbuild_parser.set_defaults(handler=lambda x: devbuild_parser.print_usage())
    devbuild = devbuild_parser.add_subparsers(title='subcommand')
    parser_trigger = devbuild.add_parser('trigger')
    parser_trigger.add_argument('product', help='eg. tidb tiflash br tidb-lightning dumpling')
    parser_trigger.add_argument('version', help='must be semantic version eg. v6.6.0-xxx')
    parser_trigger.add_argument('gitRef', help='eg. pull/1234 or master or sha1')
    parser_trigger.add_argument('-e', '--edition', choices=['community', 'enterprise'], default='community',
                                help='default is community')
    parser_trigger.add_argument('--pluginGitRef', help='only for build enterprise tidb, ignore if you dont know',
                                default='')
    parser_trigger.add_argument('--pushGCR', help='whether to push GCR, default is no', action='store_true')
    parser_trigger.add_argument('--hotfix', help=argparse.SUPPRESS, action='store_true')
    parser_trigger.add_argument('--githubRepo', help='only for the forked github repo', default='')
    parser_trigger.add_argument('--features', help='build features, eg failpoint', default='')
    parser_trigger.add_argument('--dryrun', help='dry run if you want to test', action='store_true')
    parser_trigger.add_argument('--buildEnv', help='build environment', action='append')
    parser_trigger.add_argument('--productDockerfile', help='dockerfile url for product')
    parser_trigger.add_argument('--productBaseImg', help='product base image')
    parser_trigger.add_argument('--builderImg', help='specify docker image for builder')
    parser_trigger.add_argument('--targetImg', help=argparse.SUPPRESS)
    parser_trigger.add_argument('--engine', help='pipeline engine', default='')
    parser_trigger.set_defaults(handler=trigger)
    parser_poll = devbuild.add_parser('poll')
    parser_poll.add_argument('build_id', type=int, help="the triggered build id")
    parser_poll.set_defaults(handler=lambda arg: poll(getattr(arg, 'build_id')))
    parser_rerun = devbuild.add_parser('rerun')
    parser_rerun.add_argument('build_id', type=int, help="the triggered build id")
    parser_rerun.add_argument('--dryrun', help='dry run if you want to test', action='store_true')
    parser_rerun.set_defaults(handler=rerun)
    hotfix_parser = top_subcommands.add_parser('hotfix', help='hotfix build: not implemented!')
    args = top_parser.parse_args()
    if hasattr(args, 'handler'):
        args.handler(args)
    else:
        top_parser.print_help()
