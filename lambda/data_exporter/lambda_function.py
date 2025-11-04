import json
import boto3
from datetime import datetime, timedelta
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from typing import List, Dict, Any

# Initialize logger with Powertools
logger = Logger()

# Initialize AWS clients
cloudwatch = boto3.client('cloudwatch')
ec2 = boto3.client('ec2')
s3 = boto3.client('s3')
lambda_client = boto3.client('lambda')
sns_client = boto3.client('sns')

def get_sample_instances() -> List[str]:
    """
    Get hardcoded list of EC2 instance IDs for demo purposes
    
    Returns:
        List[str]: List of EC2 instance IDs
    """
    # Hardcoded instance IDs for proof of concept
    sample_instances = [
        'i-03b933276fbf10181',
        'i-0586fae46f1e7e9ea',
        'i-038d58c996d553eaa'
    ]
    
    logger.info("Retrieved sample instances", extra={
        "instance_count": len(sample_instances),
        "instances": sample_instances
    })
    
    return sample_instances


def fetch_cloudwatch_metrics(instance_id: str) -> Dict[str, Any]:
    """
    Fetch CPU utilization metrics for a specific EC2 instance
    
    Args:
        instance_id: EC2 instance ID
        
    Returns:
        Dict containing metrics data for the instance
    """
    try:
        # Calculate current month date range
        today = datetime.now()
        first_day_current_month = today.replace(day=1)
        # Use current month data from first day until today
        
        logger.info("Fetching CloudWatch metrics", extra={
            "instance_id": instance_id,
            "start_time": first_day_current_month.isoformat(),
            "end_time": today.isoformat()
        })
        
        # Query CloudWatch for CPU utilization
        response = cloudwatch.get_metric_statistics(
            Namespace='AWS/EC2',
            MetricName='CPUUtilization',
            Dimensions=[
                {
                    'Name': 'InstanceId',
                    'Value': instance_id
                }
            ],
            StartTime=first_day_current_month,
            EndTime=today,
            Period=86400,  # Daily (24 hours in seconds)
            Statistics=['Average', 'Maximum', 'Minimum']
        )
        
        # Process and format the metrics data
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
            'cpu_data': cpu_data,
            'month': first_day_current_month.strftime('%Y-%m')
        }
        
    except Exception as e:
        logger.error("Error fetching CloudWatch metrics", extra={
            "instance_id": instance_id,
            "error": str(e)
        })
        # Return empty data structure on error to continue processing other instances
        return {
            'instance_id': instance_id,
            'cpu_data': [],
            'month': datetime.now().strftime('%Y-%m'),
            'error': str(e)
        }


def store_in_s3(metrics_data: List[Dict[str, Any]], bucket_name: str) -> str:
    """
    Store metrics data in S3 with year-month key structure
    
    Args:
        metrics_data: List of metrics data for all instances
        bucket_name: S3 bucket name
        
    Returns:
        str: S3 key where data was stored
    """
    try:
        # Create JSON structure for metrics data
        current_month = datetime.now().strftime('%Y-%m')
        
        # Use the month from the first instance's data if available
        if metrics_data and metrics_data[0].get('month'):
            current_month = metrics_data[0]['month']
        
        data_structure = {
            'month': current_month,
            'export_timestamp': datetime.now().isoformat(),
            'instances': metrics_data
        }
        
        # Create S3 key with year-month structure
        s3_key = f"data/{current_month}/metrics.json"
        
        logger.info("Storing data in S3", extra={
            "bucket": bucket_name,
            "key": s3_key,
            "instances_count": len(metrics_data)
        })
        
        # Upload to S3
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
    Invoke Report Generator Lambda function with S3 key information
    
    Args:
        s3_key: S3 key where metrics data is stored
        bucket_name: S3 bucket name
        report_generator_function_name: Name of the Report Generator Lambda function
        
    Returns:
        Dict containing invocation response
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
        
        # Invoke Report Generator Lambda asynchronously
        response = lambda_client.invoke(
            FunctionName=report_generator_function_name,
            InvocationType='Event',  # Asynchronous invocation
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


def send_process_start_notification():
    """
    Send SNS notification when the monthly process starts
    """
    try:
        import os
        sns_topic_arn = os.environ.get('SNS_TOPIC_ARN')
        if not sns_topic_arn:
            logger.warning("SNS_TOPIC_ARN environment variable not set, skipping notification")
            return
        
        subject = "CloudWatch Data Export Process Started"
        message = f"""
CloudWatch metrics data export process has started.

Process Details:
- Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
- Target Month: Current month's data
- Process: Automated monthly export

The system will export CloudWatch metrics data to S3 and generate a PDF report.
You will receive another notification when the process completes.

This is an automated message from the CloudWatch Reporting System.
        """
        
        # Send SNS notification
        response = sns_client.publish(
            TopicArn=sns_topic_arn,
            Subject=subject,
            Message=message
        )
        
        logger.info("Successfully sent process start notification", extra={
            "message_id": response['MessageId']
        })
        
    except Exception as e:
        logger.error("Error sending process start notification", extra={
            "error": str(e)
        })


def send_export_success_notification(s3_key: str, bucket_name: str, instances_count: int):
    """
    Send SNS notification when data export completes successfully
    """
    try:
        import os
        sns_topic_arn = os.environ.get('SNS_TOPIC_ARN')
        if not sns_topic_arn:
            logger.warning("SNS_TOPIC_ARN environment variable not set, skipping notification")
            return
        
        subject = "CloudWatch Data Export Completed Successfully"
        message = f"""
CloudWatch metrics data export has completed successfully.

Export Details:
- S3 Bucket: {bucket_name}
- Data Location: {s3_key}
- Instances Processed: {instances_count}
- Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

The Report Generator has been triggered and will create a PDF report shortly.
You will receive another notification when the PDF report is ready.

You can access the raw data using the following AWS CLI command:
aws s3 cp s3://{bucket_name}/{s3_key} ./metrics.json

This is an automated message from the CloudWatch Reporting System.
        """
        
        # Send SNS notification
        response = sns_client.publish(
            TopicArn=sns_topic_arn,
            Subject=subject,
            Message=message
        )
        
        logger.info("Successfully sent export success notification", extra={
            "message_id": response['MessageId']
        })
        
    except Exception as e:
        logger.error("Error sending export success notification", extra={
            "error": str(e)
        })


def lambda_handler(event: dict, context: LambdaContext) -> dict:
    """
    Main Lambda handler for CloudWatch Data Exporter
    
    Args:
        event: Lambda event data
        context: Lambda context object
        
    Returns:
        dict: Response with status and details
    """
    logger.info("Starting CloudWatch Data Exporter", extra={"event": event})
    
    try:
        # Log execution context and trigger source
        trigger_source = event.get('source', 'unknown')
        schedule_mode = event.get('schedule', 'unknown')
        
        logger.info("Lambda execution started", extra={
            "request_id": context.aws_request_id,
            "function_name": context.function_name,
            "remaining_time": context.get_remaining_time_in_millis(),
            "trigger_source": trigger_source,
            "schedule_mode": schedule_mode
        })
        
        # Send process start notification
        send_process_start_notification()
        
        # Get sample instances for demo
        instance_ids = get_sample_instances()
        
        # Fetch metrics for each instance
        all_metrics_data = []
        for instance_id in instance_ids:
            metrics_data = fetch_cloudwatch_metrics(instance_id)
            all_metrics_data.append(metrics_data)
        
        logger.info("CloudWatch metrics fetching completed", extra={
            "total_instances": len(all_metrics_data),
            "successful_fetches": len([m for m in all_metrics_data if 'error' not in m])
        })
        
        # Extract bucket name from event (EventBridge or direct invocation)
        # bucket_name = event.get('bucket_name', event.get('bucket', 'demo-cloudwatch-reports'))
        # S3 Bucket hardcoded for demo
        bucket_name = 'cloudwatch-reports-730335285545'

        # Store data in S3
        s3_key = store_in_s3(all_metrics_data, bucket_name)
        
        # Send export success notification
        send_export_success_notification(s3_key, bucket_name, len(all_metrics_data))
        
        # Determine Report Generator function name based on stack name or use default
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