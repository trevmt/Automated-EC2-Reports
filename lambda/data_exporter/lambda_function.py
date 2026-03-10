import json
import os
import re
import boto3
from datetime import datetime, timedelta
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from typing import List, Dict, Any, Optional

# Initialize logger with Powertools
logger = Logger()

# Initialize AWS clients (reporting account)
s3 = boto3.client('s3')
lambda_client = boto3.client('lambda')
sns_client = boto3.client('sns')
sts_client = boto3.client('sts')
ssm_client = boto3.client('ssm')

# Validation patterns
ACCOUNT_ID_PATTERN = re.compile(r'^\d{12}$')
REGION_PATTERN = re.compile(r'^[a-z]{2}-[a-z]+-\d$')
INSTANCE_ID_PATTERN = re.compile(r'^i-[a-f0-9]{8,17}$')


def get_target_accounts() -> List[Dict[str, Any]]:
    """
    Read the target accounts registry from SSM Parameter Store.

    Returns:
        List of account dicts with account_id, regions, and optional alias.
        Returns empty list if parameter is not set or empty.
    """
    param_name = os.environ.get('TARGET_ACCOUNTS_PARAM')
    if not param_name:
        logger.info("TARGET_ACCOUNTS_PARAM not set, no cross-account targets configured")
        return []

    try:
        response = ssm_client.get_parameter(Name=param_name)
        accounts = json.loads(response['Parameter']['Value'])

        logger.info("Loaded target accounts from SSM", extra={
            "param_name": param_name,
            "account_count": len(accounts)
        })

        # Validate each account entry
        validated = []
        for account in accounts:
            if not _validate_account_entry(account):
                logger.warning("Skipping invalid account entry", extra={"account": account})
                continue
            validated.append(account)

        return validated

    except ssm_client.exceptions.ParameterNotFound:
        logger.warning("Target accounts SSM parameter not found", extra={"param_name": param_name})
        return []
    except (json.JSONDecodeError, KeyError) as e:
        logger.error("Failed to parse target accounts parameter", extra={
            "param_name": param_name,
            "error": str(e)
        })
        return []


def _validate_account_entry(account: Dict[str, Any]) -> bool:
    """Validate an account entry from the SSM parameter to prevent injection."""
    account_id = account.get('account_id', '')
    if not ACCOUNT_ID_PATTERN.match(str(account_id)):
        logger.error("Invalid account_id format", extra={"account_id": account_id})
        return False

    regions = account.get('regions', [])
    if not isinstance(regions, list) or not regions:
        logger.error("Invalid or empty regions list", extra={"account_id": account_id})
        return False
    for region in regions:
        if not REGION_PATTERN.match(str(region)):
            logger.error("Invalid region format", extra={"account_id": account_id, "region": region})
            return False

    # Validate instance_ids if provided
    instance_filters = account.get('instance_filters')
    if instance_filters and instance_filters.get('instance_ids'):
        for iid in instance_filters['instance_ids']:
            if not INSTANCE_ID_PATTERN.match(str(iid)):
                logger.error("Invalid instance_id format in filter", extra={"instance_id": iid})
                return False

    return True


def assume_cross_account_role(account_id: str, role_name: str, external_id: str, region: str) -> boto3.Session:
    """
    Assume the cross-account IAM role and return a boto3 Session with temporary credentials.

    Args:
        account_id: Target AWS account ID
        role_name: IAM role name to assume
        external_id: External ID for the assume role call
        region: AWS region for the session

    Returns:
        boto3.Session configured with temporary credentials
    """
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    session_name = f"reporting-{account_id}-{region}"

    logger.info("Assuming cross-account role", extra={
        "role_arn": role_arn,
        "region": region
    })

    response = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        ExternalId=external_id,
        DurationSeconds=3600
    )

    credentials = response['Credentials']
    session = boto3.Session(
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken'],
        region_name=region
    )

    logger.info("Successfully assumed cross-account role", extra={
        "account_id": account_id,
        "region": region
    })
    return session


def discover_instances(ec2_client, instance_filters: Optional[Dict[str, Any]] = None) -> List[str]:
    """
    Discover EC2 instance IDs using the provided EC2 client, with optional filtering.

    Args:
        ec2_client: boto3 EC2 client (local or cross-account)
        instance_filters: Optional dict with 'instance_ids' or 'tags' to filter instances

    Returns:
        List of instance IDs
    """
    try:
        # If explicit instance IDs are provided, return them directly
        if instance_filters and instance_filters.get('instance_ids'):
            ids = instance_filters['instance_ids']
            logger.info("Using explicit instance IDs from filter", extra={"count": len(ids)})
            return ids

        # Build EC2 filters
        filters = [{'Name': 'instance-state-name', 'Values': ['running']}]

        # Add tag filters if specified
        if instance_filters and instance_filters.get('tags'):
            for tag_key, tag_value in instance_filters['tags'].items():
                filters.append({
                    'Name': f'tag:{tag_key}',
                    'Values': [tag_value] if isinstance(tag_value, str) else tag_value
                })

        paginator = ec2_client.get_paginator('describe_instances')
        instance_ids = []

        for page in paginator.paginate(Filters=filters):
            for reservation in page['Reservations']:
                for instance in reservation['Instances']:
                    instance_ids.append(instance['InstanceId'])

        logger.info("Discovered instances", extra={"count": len(instance_ids), "filters_applied": bool(instance_filters)})
        return instance_ids

    except Exception as e:
        logger.error("Error discovering instances", extra={"error": str(e)})
        return []


def fetch_cloudwatch_metrics(instance_id: str, cw_client, account_id: str = 'local', region: str = 'local') -> Dict[str, Any]:
    """
    Fetch CPU utilization metrics for a specific EC2 instance.

    Args:
        instance_id: EC2 instance ID
        cw_client: boto3 CloudWatch client (local or cross-account)
        account_id: Source account ID for labeling
        region: Source region for labeling

    Returns:
        Dict containing metrics data for the instance
    """
    try:
        today = datetime.now()
        first_day_current_month = today.replace(day=1)

        logger.info("Fetching CloudWatch metrics", extra={
            "instance_id": instance_id,
            "account_id": account_id,
            "region": region,
            "start_time": first_day_current_month.isoformat(),
            "end_time": today.isoformat()
        })

        response = cw_client.get_metric_statistics(
            Namespace='AWS/EC2',
            MetricName='CPUUtilization',
            Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
            StartTime=first_day_current_month,
            EndTime=today,
            Period=86400,
            Statistics=['Average', 'Maximum', 'Minimum']
        )

        cpu_data = []
        for datapoint in sorted(response['Datapoints'], key=lambda x: x['Timestamp']):
            cpu_data.append({
                'timestamp': datapoint['Timestamp'].strftime('%Y-%m-%d'),
                'average': round(datapoint['Average'], 2),
                'maximum': round(datapoint['Maximum'], 2),
                'minimum': round(datapoint['Minimum'], 2)
            })

        logger.info("Successfully fetched metrics", extra={
            "instance_id": instance_id,
            "datapoints_count": len(cpu_data)
        })

        return {
            'instance_id': instance_id,
            'account_id': account_id,
            'region': region,
            'cpu_data': cpu_data,
            'month': first_day_current_month.strftime('%Y-%m')
        }

    except Exception as e:
        logger.error("Error fetching CloudWatch metrics", extra={
            "instance_id": instance_id,
            "account_id": account_id,
            "error": str(e)
        })
        return {
            'instance_id': instance_id,
            'account_id': account_id,
            'region': region,
            'cpu_data': [],
            'month': datetime.now().strftime('%Y-%m'),
            'error': str(e)
        }


def fetch_metrics_for_account(account: Dict[str, Any], role_name: str, external_id: str) -> List[Dict[str, Any]]:
    """
    Fetch metrics from all regions of a single workload account.

    Args:
        account: Account dict with account_id, regions, and optional alias
        role_name: Cross-account IAM role name
        external_id: External ID for STS assume role

    Returns:
        List of metrics dicts for all instances in the account
    """
    account_id = account['account_id']
    regions = account.get('regions', ['us-east-1'])
    alias = account.get('alias', account_id)
    instance_filters = account.get('instance_filters')
    all_metrics = []

    logger.info("Processing workload account", extra={
        "account_id": account_id,
        "alias": alias,
        "regions": regions,
        "has_instance_filters": instance_filters is not None
    })

    for region in regions:
        try:
            session = assume_cross_account_role(account_id, role_name, external_id, region)
            cw_client = session.client('cloudwatch')
            ec2_client = session.client('ec2')

            instance_ids = discover_instances(ec2_client, instance_filters)

            for instance_id in instance_ids:
                metrics = fetch_cloudwatch_metrics(instance_id, cw_client, account_id, region)
                metrics['account_alias'] = alias
                all_metrics.append(metrics)

        except Exception as e:
            logger.error("Error processing account/region", extra={
                "account_id": account_id,
                "region": region,
                "error": str(e)
            })

    logger.info("Completed workload account", extra={
        "account_id": account_id,
        "instances_found": len(all_metrics)
    })
    return all_metrics


def store_in_s3(metrics_data: List[Dict[str, Any]], bucket_name: str) -> str:
    """
    Store metrics data in S3 with year-month key structure.

    Args:
        metrics_data: List of metrics data for all instances
        bucket_name: S3 bucket name

    Returns:
        str: S3 key where data was stored
    """
    try:
        current_month = datetime.now().strftime('%Y-%m')
        if metrics_data and metrics_data[0].get('month'):
            current_month = metrics_data[0]['month']

        data_structure = {
            'month': current_month,
            'export_timestamp': datetime.now().isoformat(),
            'instances': metrics_data
        }

        s3_key = f"data/{current_month}/metrics.json"

        logger.info("Storing data in S3", extra={
            "bucket": bucket_name,
            "key": s3_key,
            "instances_count": len(metrics_data)
        })

        s3.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=json.dumps(data_structure, indent=2),
            ContentType='application/json'
        )

        logger.info("Successfully stored data in S3", extra={
            "bucket": bucket_name,
            "key": s3_key
        })
        return s3_key

    except Exception as e:
        logger.error("Error storing data in S3", extra={
            "bucket": bucket_name,
            "error": str(e)
        })
        raise


def invoke_report_generator(s3_key: str, bucket_name: str, report_generator_function_name: str) -> Dict[str, Any]:
    """
    Invoke Report Generator Lambda function asynchronously.
    """
    try:
        payload = {
            'bucket_name': bucket_name,
            's3_key': s3_key,
            'source': 'data_exporter'
        }

        logger.info("Invoking Report Generator Lambda", extra={
            "function_name": report_generator_function_name,
            "payload": payload
        })

        response = lambda_client.invoke(
            FunctionName=report_generator_function_name,
            InvocationType='Event',
            Payload=json.dumps(payload)
        )

        logger.info("Successfully invoked Report Generator", extra={
            "function_name": report_generator_function_name,
            "status_code": response['StatusCode']
        })

        return {
            'invocation_status': 'success',
            'status_code': response['StatusCode']
        }

    except Exception as e:
        logger.error("Error invoking Report Generator Lambda", extra={
            "function_name": report_generator_function_name,
            "error": str(e)
        })
        return {
            'invocation_status': 'failed',
            'error': str(e)
        }


def send_process_start_notification(target_accounts: List[Dict[str, Any]]):
    """
    Send SNS notification when the monthly process starts.
    """
    try:
        sns_topic_arn = os.environ.get('SNS_TOPIC_ARN')
        if not sns_topic_arn:
            logger.warning("SNS_TOPIC_ARN not set, skipping notification")
            return

        account_list = ', '.join(
            a.get('alias', a['account_id']) for a in target_accounts
        ) if target_accounts else 'local account only'

        subject = "CloudWatch Data Export Process Started"
        message = f"""
CloudWatch metrics data export process has started.

Process Details:
- Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
- Target Month: Current month's data
- Target Accounts: {account_list}
- Process: Automated monthly export

The system will export CloudWatch metrics data to S3 and generate an HTML report.
You will receive another notification when the process completes.

This is an automated message from the CloudWatch Reporting System.
        """

        response = sns_client.publish(
            TopicArn=sns_topic_arn,
            Subject=subject,
            Message=message
        )
        logger.info("Sent process start notification", extra={"message_id": response['MessageId']})

    except Exception as e:
        logger.error("Error sending process start notification", extra={"error": str(e)})


def send_export_success_notification(s3_key: str, bucket_name: str, instances_count: int, accounts_count: int):
    """
    Send SNS notification when data export completes successfully.
    """
    try:
        sns_topic_arn = os.environ.get('SNS_TOPIC_ARN')
        if not sns_topic_arn:
            logger.warning("SNS_TOPIC_ARN not set, skipping notification")
            return

        subject = "CloudWatch Data Export Completed Successfully"
        message = f"""
CloudWatch metrics data export has completed successfully.

Export Details:
- S3 Bucket: {bucket_name}
- Data Location: {s3_key}
- Accounts Processed: {accounts_count}
- Instances Processed: {instances_count}
- Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

The Report Generator has been triggered and will create an HTML report shortly.
You will receive another notification when the report is ready.

You can access the raw data using:
aws s3 cp s3://{bucket_name}/{s3_key} ./metrics.json

This is an automated message from the CloudWatch Reporting System.
        """

        response = sns_client.publish(
            TopicArn=sns_topic_arn,
            Subject=subject,
            Message=message
        )
        logger.info("Sent export success notification", extra={"message_id": response['MessageId']})

    except Exception as e:
        logger.error("Error sending export success notification", extra={"error": str(e)})


def lambda_handler(event: dict, context: LambdaContext) -> dict:
    """
    Main Lambda handler for CloudWatch Data Exporter.

    Reads the target accounts registry from SSM, assumes cross-account roles,
    discovers instances, fetches CloudWatch metrics, stores results in S3,
    and triggers the Report Generator.
    """
    logger.info("Starting CloudWatch Data Exporter", extra={"event": event})

    try:
        trigger_source = event.get('source', 'unknown')
        schedule_mode = event.get('schedule', 'unknown')

        logger.info("Lambda execution started", extra={
            "request_id": context.aws_request_id,
            "function_name": context.function_name,
            "remaining_time": context.get_remaining_time_in_millis(),
            "trigger_source": trigger_source,
            "schedule_mode": schedule_mode
        })

        # Load cross-account configuration
        role_name = os.environ.get('CROSS_ACCOUNT_ROLE_NAME', 'CloudWatchReportingReadRole')
        external_id = os.environ.get('CROSS_ACCOUNT_EXTERNAL_ID', 'CloudWatchReporting2026')
        target_accounts = get_target_accounts()

        # Send process start notification
        send_process_start_notification(target_accounts)

        all_metrics_data = []

        # Fetch metrics from each workload account
        for account in target_accounts:
            account_metrics = fetch_metrics_for_account(account, role_name, external_id)
            all_metrics_data.extend(account_metrics)

        # If no cross-account targets configured, fall back to local account
        if not target_accounts:
            logger.info("No cross-account targets, fetching from local account")
            local_cw = boto3.client('cloudwatch')
            local_ec2 = boto3.client('ec2')
            local_account = sts_client.get_caller_identity()['Account']

            instance_ids = discover_instances(local_ec2)
            for instance_id in instance_ids:
                metrics = fetch_cloudwatch_metrics(instance_id, local_cw, local_account, os.environ.get('AWS_REGION', 'us-east-1'))
                all_metrics_data.append(metrics)

        logger.info("CloudWatch metrics fetching completed", extra={
            "total_instances": len(all_metrics_data),
            "successful_fetches": len([m for m in all_metrics_data if 'error' not in m]),
            "accounts_processed": len(target_accounts) or 1
        })

        # Store in S3
        bucket_name = os.environ.get('S3_BUCKET_NAME',
                                     event.get('bucket_name',
                                               event.get('bucket', 'demo-cloudwatch-reports')))

        s3_key = store_in_s3(all_metrics_data, bucket_name)

        # Send success notification
        send_export_success_notification(s3_key, bucket_name, len(all_metrics_data), len(target_accounts) or 1)

        # Trigger report generation
        stack_name = context.function_name.rsplit('-', 1)[0] if '-' in context.function_name else 'cloudwatch-s3-reporting'
        report_generator_function_name = event.get('report_generator_function_name', f'{stack_name}-ReportGenerator')
        invocation_result = invoke_report_generator(s3_key, bucket_name, report_generator_function_name)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Data export and report generation initiated successfully',
                'request_id': context.aws_request_id,
                's3_key': s3_key,
                'bucket_name': bucket_name,
                'instances_processed': len(all_metrics_data),
                'accounts_processed': len(target_accounts) or 1,
                'report_generator_invocation': invocation_result
            })
        }

    except Exception as e:
        logger.error("Error in Data Exporter Lambda", extra={
            "error": str(e),
            "request_id": context.aws_request_id
        })

        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': 'Internal server error',
                'request_id': context.aws_request_id
            })
        }
