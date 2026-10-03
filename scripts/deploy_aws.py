"""Deploy AsOf to AWS Lambda (serverless): about USD 0-0.50 per month.

The app sleeps when nobody uses it and wakes up by itself when someone opens the link, so the
link on your resume always works without a server running (and billing) all month.

What `deploy` does
  1. signs in with an AWS access key (asked once, hidden input; optionally saved to your standard
     AWS credentials file under the profile "asof" - never into this project)
  2. packs the code, the Dockerfile and the built index and uploads it to a private S3 bucket
  3. starts a temporary build server that builds the Docker image, pushes it to a private image
     registry (ECR) and then deletes itself (about 10-15 minutes, a few cents)
  4. creates or updates the Lambda function "asof" with your Groq / Neo4j settings from .env as
     encrypted environment variables, plus a public HTTPS link (Function URL)
  5. optionally pings the app every 5 minutes so visitors rarely wait for a cold start (free)
  6. checks /api/health and prints the link

Other commands:  status | logs | warm-on | warm-off | destroy | menu
Run:  deploy_aws.bat   (or: python scripts/deploy_aws.py deploy)
"""
from __future__ import annotations

import argparse
import base64
import configparser
import getpass
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "asof"
NAME = "asof"                      # Lambda function, ECR repository
ROLE_NAME = "asof-lambda-role"
WARM_RULE = "asof-keep-warm"
TAG = {"Key": "Project", "Value": "asof"}
DEFAULT_REGION = "ap-south-1"      # Mumbai
BUILDER_TYPE = "t3.small"          # free-plan eligible; runs ~15 minutes per deploy
BUILD_TIMEOUT_MIN = 60
SKIP_ENV = {"ASOF_DATA_DIR"}       # local-only settings that must not go to the cloud
LAMBDA_ENV = {
    "HOME": "/tmp", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    "AWS_LWA_PORT": "7860", "AWS_LWA_ASYNC_INIT": "true",
    "ASOF_CLIENT_IP_HEADER": "x-forwarded-for",
}

# Runs on the temporary build server. No "set -x": the registry token and links must not be logged.
BUILDER_SCRIPT = r"""#!/bin/bash
exec > >(tee /var/log/asof-build.log) 2>&1
set -euo pipefail
shutdown -h +75 || true   # safety net: the server removes itself after 75 minutes whatever happens
finish() {
  code=$?
  echo "ASOF: build script exit code $code"
  sleep 2
  curl -s -X PUT --upload-file /var/log/asof-build.log '__LOGURL__' || true
  shutdown -h now
}
trap finish EXIT
echo "ASOF: build server started"
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
fi
dnf install -y -q docker
systemctl start docker
for i in 1 2 3 4 5; do curl -fsSL -o /root/bundle.tar.gz '__URL__' && break; echo "ASOF: download retry $i"; sleep 15; done
mkdir -p /root/build && tar xzf /root/bundle.tar.gz -C /root/build
cd /root/build
echo "ASOF: building the image (about 10 minutes)"
# classic builder => a plain single-platform image manifest, which Lambda requires
DOCKER_BUILDKIT=0 docker build -t '__IMAGE__' .
echo "ASOF: pushing the image"
echo '__TOKEN__' | docker login --username AWS --password-stdin '__REGISTRY__' >/dev/null
docker push '__IMAGE__' | tail -n 3
echo "ASOF: BUILD OK"
"""


# ----------------------------------------------------------------------------- helpers
LOG_FILE = ROOT / "logs" / "deploy_aws.log"


def say(msg: str = "") -> None:
    """Print, and keep a copy in logs/deploy_aws.log so nothing is lost if the window closes."""
    print(msg, flush=True)
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def latest_image(ecr) -> dict | None:
    """Newest image in the registry (tag + push time), if any."""
    try:
        details = []
        for page in ecr.get_paginator("describe_images").paginate(repositoryName=NAME):
            details += [d for d in page["imageDetails"] if d.get("imageTags")]
    except Exception:
        return None
    return max(details, key=lambda d: d["imagePushedAt"]) if details else None


def data_dir() -> Path:
    if os.getenv("ASOF_DATA_DIR"):
        return Path(os.environ["ASOF_DATA_DIR"])
    local = Path(os.getenv("LOCALAPPDATA", "")) / "asof" / "data"
    return local if local.exists() else ROOT / "data"


def _credentials_file() -> Path:
    return Path(os.getenv("AWS_SHARED_CREDENTIALS_FILE") or Path.home() / ".aws" / "credentials")


def get_session(region: str):
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    def works(s) -> str | None:
        try:
            return s.client("sts").get_caller_identity()["Arn"]
        except (BotoCoreError, ClientError):
            return None

    candidates = []
    if PROFILE in boto3.Session().available_profiles:
        candidates.append(boto3.Session(profile_name=PROFILE, region_name=region))
    candidates.append(boto3.Session(region_name=region))
    for s in candidates:
        arn = works(s)
        if arn:
            say(f"Signed in to AWS as {arn}")
            return s

    say("AWS sign-in needed. Use the access key of the IAM user you created for this (see SETUP_GUIDE section 9).")
    for _ in range(3):
        key_id = input("  Access key ID: ").strip()
        secret = getpass.getpass("  Secret access key (hidden, press Enter after pasting): ").strip()
        s = boto3.Session(aws_access_key_id=key_id, aws_secret_access_key=secret, region_name=region)
        arn = works(s)
        if arn:
            say(f"Signed in to AWS as {arn}")
            if input(f"  Save this key in {_credentials_file()} (profile '{PROFILE}') so you are not asked again? [Y/n] ").strip().lower() in ("", "y", "yes"):
                path = _credentials_file()
                path.parent.mkdir(parents=True, exist_ok=True)
                cp = configparser.ConfigParser()
                cp.read(path)
                cp[PROFILE] = {"aws_access_key_id": key_id, "aws_secret_access_key": secret}
                with open(path, "w") as f:
                    cp.write(f)
                say("  Saved.")
            return s
        say("  That key did not work - check both values and try again.")
    raise SystemExit("Could not sign in to AWS.")


def _code(e) -> str:
    return getattr(e, "response", {}).get("Error", {}).get("Code", "")


def _s3_config():
    from botocore.config import Config

    return Config(signature_version="s3v4", s3={"addressing_style": "virtual"})


def http_json(url: str, timeout: float = 150) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def function_url(lam) -> str | None:
    try:
        return lam.get_function_url_config(FunctionName=NAME)["FunctionUrl"]
    except Exception:
        return None


# ----------------------------------------------------------------------------- bundle + settings
def app_env() -> dict:
    from dotenv import dotenv_values

    env = {k: v for k, v in dotenv_values(ROOT / ".env").items()
           if v is not None and k not in SKIP_ENV and "\n" not in v}
    missing = [k for k in ("GROQ_API_KEY", "NEO4J_URI", "NEO4J_PASSWORD") if not env.get(k)]
    if missing:
        say(f"WARNING: {', '.join(missing)} not set in .env - the app will use its offline fallback for those.")
    env.setdefault("TOP_K", "6")
    env.setdefault("ASK_LIMIT_PER_HOUR", "30")  # protects your Groq free quota from heavy visitors
    env.update(LAMBDA_ENV)
    return env


def make_bundle(out: Path) -> float:
    src_data = data_dir()
    say(f"Packing code + index from {src_data} ...")
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        for name in ("Dockerfile", "requirements.txt", "pyproject.toml", ".dockerignore"):
            shutil.copy2(ROOT / name, t / name)
        shutil.copytree(ROOT / "src", t / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        for sub, skip in (("processed", ()), ("index", ("embedding_cache.npz",))):
            src = src_data / sub
            if not src.exists():
                raise SystemExit(f"Missing {src} - run rebuild.bat first.")
            (t / "data" / sub).mkdir(parents=True)
            for f in src.iterdir():
                if f.is_file() and f.name not in skip:
                    shutil.copy2(f, t / "data" / sub / f.name)
        with tarfile.open(out, "w:gz") as tar:
            for p in sorted(t.iterdir()):
                tar.add(p, arcname=p.name)
    return out.stat().st_size / 1e6


# ----------------------------------------------------------------------------- AWS resources
def ensure_bucket(s3, account: str, region: str) -> str:
    from botocore.exceptions import ClientError

    bucket = f"asof-deploy-{account}-{region}"
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        kw = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        s3.create_bucket(Bucket=bucket, **kw)
        s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    return bucket


def ensure_repo(ecr) -> str:
    from botocore.exceptions import ClientError

    try:
        uri = ecr.describe_repositories(repositoryNames=[NAME])["repositories"][0]["repositoryUri"]
    except ClientError as e:
        if _code(e) != "RepositoryNotFoundException":
            raise
        uri = ecr.create_repository(repositoryName=NAME, tags=[TAG])["repository"]["repositoryUri"]
    # keep only the 2 newest images (current + previous) so storage stays at a few cents a month
    ecr.put_lifecycle_policy(repositoryName=NAME, lifecyclePolicyText=json.dumps({"rules": [{
        "rulePriority": 1, "description": "keep the 2 newest images",
        "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 2},
        "action": {"type": "expire"}}]}))
    return uri


def ensure_role(iam) -> str:
    from botocore.exceptions import ClientError

    try:
        return iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    except ClientError as e:
        if _code(e) != "NoSuchEntity":
            raise
    arn = iam.create_role(RoleName=ROLE_NAME, Tags=[TAG], Description="Runs the AsOf Lambda function",
                          AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                              "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                              "Action": "sts:AssumeRole"}]}))["Role"]["Arn"]
    iam.attach_role_policy(RoleName=ROLE_NAME,
                           PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    time.sleep(12)  # a brand-new role takes a few seconds before Lambda can use it
    return arn


def latest_ami(ec2) -> tuple[str, str]:
    imgs = ec2.describe_images(Owners=["amazon"], Filters=[
        {"Name": "name", "Values": ["al2023-ami-2023.*-kernel-*-x86_64"]},
        {"Name": "state", "Values": ["available"]}, {"Name": "architecture", "Values": ["x86_64"]}])["Images"]
    if not imgs:
        raise SystemExit("Could not find the Amazon Linux 2023 image in this region.")
    img = max(imgs, key=lambda i: i["CreationDate"])
    return img["ImageId"], img.get("RootDeviceName", "/dev/xvda")


def builder_instances(ec2, states=("pending", "running", "stopping", "stopped")) -> list[dict]:
    res = ec2.describe_instances(Filters=[{"Name": f"tag:{TAG['Key']}", "Values": [TAG["Value"]]},
                                          {"Name": "tag:Role", "Values": ["builder"]},
                                          {"Name": "instance-state-name", "Values": list(states)}])
    return [i for r in res["Reservations"] for i in r["Instances"]]


def build_image(sess, account: str, region: str, image: str) -> None:
    ec2, s3, ecr = sess.client("ec2"), sess.client("s3", config=_s3_config()), sess.client("ecr")
    stamp = image.rsplit(":", 1)[1]
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "asof-bundle.tar.gz"
        mb = make_bundle(bundle)
        bucket = ensure_bucket(s3, account, region)
        key, log_key = f"bundles/{stamp}.tar.gz", f"logs/{stamp}.log"
        say(f"Uploading {mb:.0f} MB to your private S3 bucket {bucket} ...")
        s3.upload_file(str(bundle), bucket, key)
    get_url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3 * 3600)
    log_url = s3.generate_presigned_url("put_object", Params={"Bucket": bucket, "Key": log_key}, ExpiresIn=3 * 3600)
    auth = ecr.get_authorization_token()["authorizationData"][0]
    password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)[1]
    registry = auth["proxyEndpoint"].replace("https://", "")
    script = (BUILDER_SCRIPT.replace("__URL__", get_url).replace("__LOGURL__", log_url)
              .replace("__IMAGE__", image).replace("__REGISTRY__", registry).replace("__TOKEN__", password))

    ami, root_dev = latest_ami(ec2)
    tags = [TAG, {"Key": "Role", "Value": "builder"}, {"Key": "Name", "Value": f"asof-builder-{stamp}"}]
    inst = ec2.run_instances(
        ImageId=ami, InstanceType=BUILDER_TYPE, MinCount=1, MaxCount=1, UserData=script,
        InstanceInitiatedShutdownBehavior="terminate",
        BlockDeviceMappings=[{"DeviceName": root_dev, "Ebs": {"VolumeSize": 20, "VolumeType": "gp3",
                                                              "DeleteOnTermination": True, "Encrypted": True}}],
        MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags}, {"ResourceType": "volume", "Tags": tags}],
    )["Instances"][0]
    iid = inst["InstanceId"]
    say(f"Build server {iid} started. Building the app image - about 10-15 minutes (a line every minute) ...")

    def image_ready() -> bool:
        try:
            ecr.describe_images(repositoryName=NAME, imageIds=[{"imageTag": stamp}])
            return True
        except Exception:
            return False

    def build_log() -> str:
        try:
            return s3.get_object(Bucket=bucket, Key=log_key)["Body"].read().decode(errors="replace")
        except Exception:
            return ""

    start, ok = time.time(), False
    while time.time() - start < BUILD_TIMEOUT_MIN * 60:
        time.sleep(60)
        if image_ready():
            ok = True
            break
        state = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]["State"]["Name"]
        if state in ("shutting-down", "terminated"):
            time.sleep(20)
            ok = image_ready()
            break
        say(f"  ... {int((time.time() - start) / 60)} min")
    log = ""
    for _ in range(6):  # the build server uploads its log as its very last step
        log = build_log()
        if log or ok:
            break
        time.sleep(10)
    try:  # full build log, kept on your laptop
        LOG_FILE.parent.mkdir(exist_ok=True)
        (LOG_FILE.parent / "aws_build.log").write_text(log or "(no log was uploaded)", encoding="utf-8")
    except OSError:
        pass
    for k in ((key, log_key) if ok else (key,)):  # on failure the log stays in S3 for 'status'
        try:
            s3.delete_object(Bucket=bucket, Key=k)
        except Exception:
            pass
    if not ok:
        try:
            ec2.terminate_instances(InstanceIds=[iid])
        except Exception:
            pass
        say("\nThe image build failed. Last lines of the build log:\n")
        say("\n".join(log.splitlines()[-60:]) or "(no log was uploaded)")
        say("\nNothing was changed on the live app. The full build log is in logs/aws_build.log.")
        sys.exit(1)
    say("  image built and stored.")


def deploy_function(lam, role_arn: str, image: str, env: dict, memory: int) -> None:
    from botocore.exceptions import ClientError

    cfg = dict(Role=role_arn, Timeout=300, MemorySize=memory, Environment={"Variables": env},
               Description="AsOf - time-aware GraphRAG over RBI regulations")
    exists = True
    try:
        current = lam.get_function(FunctionName=NAME)["Configuration"]
    except ClientError as e:
        if _code(e) != "ResourceNotFoundException":
            raise
        exists = False

    if exists:
        say("Updating the Lambda function (the live app keeps serving until the switch) ...")
        if memory == 3008:  # keep whatever memory the first deploy settled on
            cfg["MemorySize"] = current.get("MemorySize", memory)
        lam.update_function_code(FunctionName=NAME, ImageUri=image)
        lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
        lam.update_function_configuration(FunctionName=NAME, **cfg)
        lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
        return

    say("Creating the Lambda function ...")
    for attempt in range(8):
        try:
            lam.create_function(FunctionName=NAME, PackageType="Image", Code={"ImageUri": image},
                                Architectures=["x86_64"], Tags={TAG["Key"]: TAG["Value"]}, **cfg)
            break
        except ClientError as e:
            msg = str(e)
            if _code(e) == "InvalidParameterValueException" and "MemorySize" in msg and cfg["MemorySize"] > 2048:
                say("  this account allows less memory - using 2048 MB")
                cfg["MemorySize"] = 2048
            elif _code(e) == "InvalidParameterValueException" and "role" in msg.lower() and attempt < 7:
                time.sleep(8)  # the new role is still propagating
            else:
                raise
    say("  waiting for AWS to prepare the function (a few minutes the first time) ...")
    lam.get_waiter("function_active_v2").wait(FunctionName=NAME, WaiterConfig={"Delay": 10, "MaxAttempts": 90})


def ensure_public_url(lam) -> str:
    from botocore.exceptions import ClientError

    url = function_url(lam)
    if not url:
        url = lam.create_function_url_config(FunctionName=NAME, AuthType="NONE")["FunctionUrl"]
    # public links need two permissions (AWS rule since Oct 2025): invoke-the-URL and invoke-via-the-URL
    for sid, kw in (("public-url", {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"}),
                    ("public-invoke-via-url", {"Action": "lambda:InvokeFunction", "InvokedViaFunctionUrl": True})):
        try:
            lam.add_permission(FunctionName=NAME, StatementId=sid, Principal="*", **kw)
        except ClientError as e:
            if _code(e) != "ResourceConflictException":  # already added
                raise
    return url


def set_keep_warm(sess, on: bool) -> None:
    from botocore.exceptions import ClientError

    ev, lam = sess.client("events"), sess.client("lambda")
    if not on:
        try:
            ev.remove_targets(Rule=WARM_RULE, Ids=["asof"])
            ev.delete_rule(Name=WARM_RULE)
        except ClientError:
            pass
        say("Keep-warm is OFF: the app still works, but the first visit after a quiet spell takes ~30-60 s.")
        return
    fn_arn = lam.get_function(FunctionName=NAME)["Configuration"]["FunctionArn"]
    rule_arn = ev.put_rule(Name=WARM_RULE, ScheduleExpression="rate(5 minutes)", State="ENABLED",
                           Description="Keeps the AsOf app loaded")["RuleArn"]
    try:
        lam.add_permission(FunctionName=NAME, StatementId="keep-warm", Action="lambda:InvokeFunction",
                           Principal="events.amazonaws.com", SourceArn=rule_arn)
    except ClientError as e:
        if _code(e) != "ResourceConflictException":
            raise
    ev.put_targets(Rule=WARM_RULE, Targets=[{"Id": "asof", "Arn": fn_arn, "Input": json.dumps({"asof": "warm"})}])
    say("Keep-warm is ON: the app is pinged every 5 minutes so visitors rarely wait (stays inside the free tier).")


# ----------------------------------------------------------------------------- commands
def cmd_deploy(sess, a) -> None:
    account = sess.client("sts").get_caller_identity()["Account"]
    region = a.region
    env = app_env()
    ecr = sess.client("ecr")
    repo_uri = ensure_repo(ecr)
    image = None
    prev = None if a.rebuild else latest_image(ecr)
    if prev:
        age_min = (datetime.now(timezone.utc) - prev["imagePushedAt"]).total_seconds() / 60
        if age_min < 24 * 60:
            ans = input(f"An app image was already built {age_min:.0f} minutes ago. Use it and skip the "
                        f"15-minute build? (answer n if you changed code or rebuilt the index since) [Y/n] ")
            if ans.strip().lower() in ("", "y", "yes"):
                image = f"{repo_uri}:{prev['imageTags'][0]}"
                say(f"Using the existing image {prev['imageTags'][0]}.")
    if image is None:
        stamp = datetime.now(timezone.utc).strftime("v%Y%m%d-%H%M%S")
        image = f"{repo_uri}:{stamp}"
        build_image(sess, account, region, image)

    lam = sess.client("lambda")
    role = ensure_role(sess.client("iam"))
    deploy_function(lam, role, image, env, a.memory)
    url = ensure_public_url(lam)
    if not a.no_warm:
        set_keep_warm(sess, True)

    say("Checking the app (the first wake-up can take 1-2 minutes) ...")
    h = None
    for _ in range(4):
        h = http_json(url.rstrip("/") + "/api/health", timeout=150)
        if h:
            break
        time.sleep(10)
    if h:
        say(f"  healthy: {h.get('documents')} documents, LLM {h.get('llm')}, graph {h.get('graph_backend')}")
    else:
        say("  the app did not answer yet - open the link in a minute; if it still fails, run aws_app.bat -> logs")
    say("\n" + "=" * 72)
    say(f"  AsOf is live:  {url}")
    say("=" * 72)
    say("This link stays the same on every re-deploy. Put it on your resume and in the README.")


def show_last_build_log(sess, region: str) -> None:
    """Print the newest build-server log left in the deploy bucket (kept when a deploy was interrupted)."""
    s3 = sess.client("s3", config=_s3_config())
    account = sess.client("sts").get_caller_identity()["Account"]
    bucket = f"asof-deploy-{account}-{region}"
    try:
        logs = [o for o in s3.list_objects_v2(Bucket=bucket, Prefix="logs/").get("Contents", [])]
    except Exception:
        logs = []
    if not logs:
        say("  No build-server log was found.")
        return
    newest = max(logs, key=lambda o: o["LastModified"])
    text = s3.get_object(Bucket=bucket, Key=newest["Key"])["Body"].read().decode(errors="replace")
    say(f"  Last build-server log ({newest['Key']}), final lines:\n")
    say("\n".join(text.splitlines()[-80:]))


def cmd_status(sess, a) -> None:
    from botocore.exceptions import ClientError

    lam = sess.client("lambda")
    try:
        cfg = lam.get_function(FunctionName=NAME)["Configuration"]
    except ClientError:
        say(f"The AsOf app (Lambda function) is not set up in {a.region} yet.")
        img = latest_image(sess.client("ecr"))
        if img:
            say(f"  An app image was built at {img['imagePushedAt']:%d %b %H:%M} UTC - run deploy_aws.bat and answer Y to reuse it.")
        builders = builder_instances(sess.client("ec2"), ("pending", "running"))
        if builders:
            say("  A build server is still running - wait a few minutes, then run deploy_aws.bat and reuse the image.")
        show_last_build_log(sess, a.region)
        return
    url = function_url(lam)
    say(f"Function: {NAME}  memory {cfg.get('MemorySize')} MB  last deployed {cfg.get('LastModified', '?')[:19]}")
    try:
        warm = sess.client("events").describe_rule(Name=WARM_RULE)["State"] == "ENABLED"
    except ClientError:
        warm = False
    say(f"Keep-warm: {'on' if warm else 'off'}")
    if url:
        say(f"Link: {url}")
        say("Checking the app (may take up to a minute if it was asleep) ...")
        h = http_json(url.rstrip("/") + "/api/health", timeout=150)
        say(f"  {'healthy - ' + str(h.get('documents')) + ' documents' if h else 'not answering - see logs'}")


def cmd_logs(sess, a) -> None:
    logs = sess.client("logs")
    try:
        ev = logs.filter_log_events(logGroupName=f"/aws/lambda/{NAME}",
                                    startTime=int((time.time() - 3 * 3600) * 1000), limit=200)["events"]
    except Exception as e:
        say(f"No logs yet ({type(e).__name__}).")
        return
    lines = [e["message"].rstrip() for e in ev if not e["message"].startswith(("START", "END", "REPORT"))]
    say("\n".join(lines[-80:]) or "No log lines in the last 3 hours.")


def cmd_warm_on(sess, a) -> None:
    set_keep_warm(sess, True)


def cmd_warm_off(sess, a) -> None:
    set_keep_warm(sess, False)


def cmd_destroy(sess, a) -> None:
    from botocore.exceptions import ClientError

    if input("This deletes the AsOf app and everything this script created on AWS. Type 'destroy' to confirm: ").strip() != "destroy":
        say("Cancelled.")
        return
    set_keep_warm(sess, False)
    lam, ecr, iam, ec2 = sess.client("lambda"), sess.client("ecr"), sess.client("iam"), sess.client("ec2")
    s3 = sess.client("s3", config=_s3_config())
    for step, fn in (
        ("function", lambda: lam.delete_function(FunctionName=NAME)),
        ("image registry", lambda: ecr.delete_repository(repositoryName=NAME, force=True)),
        ("role", lambda: (iam.detach_role_policy(RoleName=ROLE_NAME, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"),
                          iam.delete_role(RoleName=ROLE_NAME))),
    ):
        try:
            fn()
            say(f"  deleted {step}")
        except ClientError:
            pass
    ids = [i["InstanceId"] for i in builder_instances(ec2)]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
    account = sess.client("sts").get_caller_identity()["Account"]
    bucket = f"asof-deploy-{account}-{a.region}"
    try:
        for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
            s3.delete_object(Bucket=bucket, Key=obj["Key"])
        s3.delete_bucket(Bucket=bucket)
        say("  deleted bucket")
    except ClientError:
        pass
    say("Done. Nothing AsOf-related is left on AWS, so nothing more is charged for it.")


COMMANDS = {"deploy": cmd_deploy, "status": cmd_status, "logs": cmd_logs, "warm-on": cmd_warm_on,
            "warm-off": cmd_warm_off, "destroy": cmd_destroy}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="deploy", choices=[*COMMANDS, "menu"])
    p.add_argument("--region", default=os.getenv("ASOF_AWS_REGION", DEFAULT_REGION))
    p.add_argument("--memory", type=int, default=3008, help="Lambda memory in MB (default 3008)")
    p.add_argument("--no-warm", action="store_true", help="do not turn on the 5-minute keep-warm ping")
    p.add_argument("--rebuild", action="store_true", help="always build a new image (do not offer to reuse one)")
    a = p.parse_args()
    if a.command == "menu":
        say("AsOf on AWS:  1) status + link   2) recent app log   3) keep-warm ON   4) keep-warm OFF   5) delete everything")
        a.command = {"1": "status", "2": "logs", "3": "warm-on", "4": "warm-off", "5": "destroy"}.get(input("Choose 1-5: ").strip())
        if not a.command:
            return
    say(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S}  {a.command} ===")
    sess = get_session(a.region)
    try:
        COMMANDS[a.command](sess, a)
    except Exception:
        import traceback

        say("\nERROR (also saved in logs/deploy_aws.log):\n" + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
