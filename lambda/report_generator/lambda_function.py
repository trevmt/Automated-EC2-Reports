import json
import boto3
import os
from datetime import datetime
from aws_lambda_powertools import Logger
import pandas as pd
import tempfile

# Initialize logger
logger = Logger()

# Initialize AWS clients
s3_client = boto3.client('s3')
sns_client = boto3.client('sns')


def lambda_handler(event, context):
    """
    Lambda handler for Report Generator using AWS built-in layers.
    """
    logger.info("Report Generator Lambda started", extra={"event": event})

    try:
        bucket_name = event.get('bucket_name') or os.environ.get('S3_BUCKET_NAME')
        data_key = event.get('s3_key')

        if not bucket_name or not data_key:
            raise ValueError("Missing required parameters: bucket_name or s3_key")

        logger.info("Processing report generation", extra={
            "bucket_name": bucket_name,
            "data_key": data_key
        })

        report_key = generate_pandas_report(bucket_name, data_key)
        send_notification(bucket_name, report_key, success=True)

        logger.info("Report generation completed successfully", extra={"report_key": report_key})

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Report generated successfully',
                'report_key': report_key
            })
        }

    except Exception as e:
        logger.error("Report generation failed", extra={"error": str(e)})
        send_notification(None, None, success=False, error=str(e))

        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'Report generation failed',
                'error': str(e)
            })
        }


def generate_pandas_report(bucket_name, data_key):
    """Generate HTML report using pandas for data processing."""
    metrics_data = download_metrics_data(bucket_name, data_key)
    df = process_metrics_with_pandas(metrics_data)
    html_path = create_html_report(df, metrics_data)
    report_key = upload_report_to_s3(bucket_name, html_path, metrics_data['month'])
    return report_key


def download_metrics_data(bucket_name, data_key):
    """Download and parse metrics data from S3."""
    logger.info("Downloading metrics data from S3", extra={
        "bucket": bucket_name, "key": data_key
    })

    try:
        response = s3_client.get_object(Bucket=bucket_name, Key=data_key)
        data = json.loads(response['Body'].read().decode('utf-8'))

        logger.info("Successfully downloaded metrics data", extra={
            "instances_count": len(data.get('instances', [])),
            "month": data.get('month')
        })
        return data

    except Exception as e:
        logger.error("Error downloading metrics data", extra={
            "bucket": bucket_name, "key": data_key, "error": str(e)
        })
        raise


def process_metrics_with_pandas(metrics_data):
    """
    Process metrics data using pandas for analysis.
    Now includes account_id, account_alias, and region columns for multi-account support.
    """
    logger.info("Processing metrics data with pandas")

    rows = []
    for instance_data in metrics_data.get('instances', []):
        instance_id = instance_data['instance_id']
        account_id = instance_data.get('account_id', 'local')
        account_alias = instance_data.get('account_alias', account_id)
        region = instance_data.get('region', 'unknown')

        for datapoint in instance_data.get('cpu_data', []):
            rows.append({
                'account_id': account_id,
                'account_alias': account_alias,
                'region': region,
                'instance_id': instance_id,
                'date': datapoint['timestamp'],
                'avg_cpu': datapoint['average'],
                'max_cpu': datapoint['maximum'],
                'min_cpu': datapoint['minimum']
            })

    expected_cols = [
        'account_id', 'account_alias', 'region', 'instance_id',
        'date', 'avg_cpu', 'max_cpu', 'min_cpu'
    ]

    if not rows:
        df = pd.DataFrame(columns=expected_cols)
    else:
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'])

    logger.info("Successfully processed data with pandas", extra={
        "rows": len(df),
        "accounts": df['account_id'].nunique() if not df.empty else 0
    })
    return df


def create_html_report(df, metrics_data):
    """
    Create HTML report with multi-account grouping.
    """
    logger.info("Creating HTML report")

    try:
        html_path = tempfile.mktemp(suffix='.html')
        month = metrics_data.get('month', 'Unknown Month')
        is_multi_account = df['account_id'].nunique() > 1 if not df.empty else False

        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>CloudWatch CPU Utilization Report - {month}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        h1 {{ color: #232F3E; text-align: center; }}
        h2 {{ color: #FF9900; border-bottom: 2px solid #FF9900; padding-bottom: 5px; }}
        h3 {{ color: #232F3E; margin-top: 30px; }}
        .summary {{ background-color: #f8f9fa; padding: 20px; border-radius: 5px; margin: 20px 0; }}
        .account-section {{ border-left: 4px solid #FF9900; padding-left: 20px; margin: 30px 0; }}
        table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
        th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
        th {{ background-color: #232F3E; color: white; }}
        tr:nth-child(even) {{ background-color: #f2f2f2; }}
        .metric {{ display: inline-block; margin: 10px 20px; }}
        .account-header {{ background-color: #232F3E; color: white; padding: 10px 20px; border-radius: 5px 5px 0 0; margin-top: 30px; }}
    </style>
</head>
<body>
    <h1>CloudWatch CPU Utilization Report</h1>
    <h2>Report Period: {month}</h2>
"""

        if not df.empty:
            html_content += _build_executive_summary(df, is_multi_account)

            if is_multi_account:
                html_content += _build_account_overview_table(df)

            # Per-account sections
            for (account_id, account_alias), account_df in df.groupby(['account_id', 'account_alias']):
                html_content += _build_account_section(account_id, account_alias, account_df, is_multi_account)

            html_content += _build_recommendations(df, is_multi_account)
        else:
            html_content += """
    <div class="summary">
        <h2>No Data Available</h2>
        <p>No CPU utilization data was found for the specified time period.</p>
    </div>
"""

        html_content += f"""
    <div style="margin-top: 40px; text-align: center; color: #666; font-size: 12px;">
        Report generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')} by CloudWatch S3 Reporting System
    </div>
</body>
</html>"""

        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        logger.info("Successfully created HTML report", extra={"html_path": html_path})
        return html_path

    except Exception as e:
        logger.error("Error creating HTML report", extra={"error": str(e)})
        raise


def _build_executive_summary(df, is_multi_account):
    """Build the executive summary section."""
    total_accounts = df['account_id'].nunique()
    total_instances = df['instance_id'].nunique()
    avg_cpu = df['avg_cpu'].mean()
    max_cpu = df['max_cpu'].max()
    min_cpu = df['min_cpu'].min()

    html = """
    <div class="summary">
        <h2>Executive Summary</h2>
"""
    if is_multi_account:
        html += f'        <div class="metric">Total Accounts: <strong>{total_accounts}</strong></div>\n'

    html += f"""        <div class="metric">Total Instances: <strong>{total_instances}</strong></div>
        <div class="metric">Overall Average CPU: <strong>{avg_cpu:.2f}%</strong></div>
        <div class="metric">Peak CPU Usage: <strong>{max_cpu:.2f}%</strong></div>
        <div class="metric">Minimum CPU Usage: <strong>{min_cpu:.2f}%</strong></div>
    </div>
"""
    return html


def _build_account_overview_table(df):
    """Build a cross-account summary table (only shown for multi-account reports)."""
    account_summary = df.groupby(['account_id', 'account_alias']).agg({
        'instance_id': 'nunique',
        'avg_cpu': 'mean',
        'max_cpu': 'max',
        'min_cpu': 'min',
        'region': lambda x: ', '.join(sorted(x.unique()))
    }).round(2).reset_index()

    account_summary.columns = [
        'Account ID', 'Account Alias', 'Instances',
        'Avg CPU (%)', 'Max CPU (%)', 'Min CPU (%)', 'Regions'
    ]

    html = "    <h2>Account Overview</h2>\n"
    html += account_summary.to_html(classes='summary-table', escape=False, index=False)
    html += "\n"
    return html


def _build_account_section(account_id, account_alias, account_df, is_multi_account):
    """Build the per-account detail section with instance summary and daily data."""
    if is_multi_account:
        label = f"{account_alias} ({account_id})"
        html = f'    <div class="account-section">\n'
        html += f'    <div class="account-header">{label}</div>\n'
    else:
        html = ""

    # Instance summary table
    instance_summary = account_df.groupby('instance_id').agg({
        'avg_cpu': ['mean', 'std'],
        'max_cpu': 'max',
        'min_cpu': 'min',
        'date': 'count'
    }).round(2)

    instance_summary.columns = ['Avg CPU (%)', 'CPU StdDev', 'Max CPU (%)', 'Min CPU (%)', 'Data Points']
    instance_summary = instance_summary.reset_index()
    instance_summary.columns = ['Instance ID'] + list(instance_summary.columns[1:])

    html += "    <h3>Instance Summary</h3>\n"
    html += instance_summary.to_html(classes='summary-table', escape=False, index=False)

    # Daily detail table
    df_display = account_df.copy()
    df_display['avg_cpu'] = df_display['avg_cpu'].round(2)
    df_display['max_cpu'] = df_display['max_cpu'].round(2)
    df_display['min_cpu'] = df_display['min_cpu'].round(2)
    df_display['date'] = df_display['date'].dt.strftime('%Y-%m-%d')

    display_cols = ['instance_id', 'region', 'date', 'avg_cpu', 'max_cpu', 'min_cpu']
    display_names = ['Instance ID', 'Region', 'Date', 'Average CPU (%)', 'Maximum CPU (%)', 'Minimum CPU (%)']

    df_display = df_display[display_cols]
    df_display.columns = display_names

    html += "    <h3>Daily CPU Utilization</h3>\n"
    html += df_display.to_html(classes='detail-table', escape=False, index=False)

    if is_multi_account:
        html += "    </div>\n"

    return html


def _build_recommendations(df, is_multi_account):
    """Build the recommendations section, grouped by account if multi-account."""
    html = "    <h2>Recommendations</h2>\n"

    if is_multi_account:
        for (account_id, account_alias), account_df in df.groupby(['account_id', 'account_alias']):
            html += f"    <h3>{account_alias} ({account_id})</h3>\n    <ul>\n"
            html += _recommendations_for_instances(account_df)
            html += "    </ul>\n"
    else:
        html += "    <ul>\n"
        html += _recommendations_for_instances(df)
        html += "    </ul>\n"

    return html


def _recommendations_for_instances(df):
    """Generate recommendation list items for instances in a DataFrame."""
    html = ""
    for instance_id in df['instance_id'].unique():
        instance_df = df[df['instance_id'] == instance_id]
        avg_cpu = instance_df['avg_cpu'].mean()
        max_cpu = instance_df['max_cpu'].max()
        rec = generate_recommendations(avg_cpu, max_cpu)
        html += f"        <li><strong>{instance_id}:</strong> {rec}</li>\n"
    return html


def generate_recommendations(avg_cpu, max_cpu):
    """Generate basic recommendations based on CPU usage patterns."""
    if avg_cpu < 20:
        return "CPU utilization is low. Consider downsizing the instance type to reduce costs."
    elif avg_cpu > 80:
        return "CPU utilization is high. Consider upgrading to a larger instance type or implementing auto-scaling."
    elif max_cpu > 95:
        return "CPU spikes detected. Monitor for performance issues and consider implementing auto-scaling."
    else:
        return "CPU utilization appears to be within normal ranges."


def upload_report_to_s3(bucket_name, html_path, month):
    """Upload HTML report to S3 reports folder."""
    logger.info("Uploading HTML report to S3")

    try:
        report_key = f"reports/{month}-report.html"

        with open(html_path, 'rb') as html_file:
            s3_client.put_object(
                Bucket=bucket_name,
                Key=report_key,
                Body=html_file,
                ContentType='text/html'
            )

        logger.info("Successfully uploaded HTML report to S3", extra={
            "bucket": bucket_name, "key": report_key
        })
        return report_key

    except Exception as e:
        logger.error("Error uploading HTML report to S3", extra={
            "bucket": bucket_name, "error": str(e)
        })
        raise
    finally:
        if os.path.exists(html_path):
            os.remove(html_path)


def send_notification(bucket_name, report_key, success=True, error=None):
    """Send SNS notification about report status."""
    try:
        sns_topic_arn = os.environ.get('SNS_TOPIC_ARN')
        if not sns_topic_arn:
            logger.warning("SNS_TOPIC_ARN not set, skipping notification")
            return

        if success:
            subject = "CloudWatch Report Generated Successfully"
            message = f"""
CloudWatch CPU Utilization Report has been generated successfully.

Report Details:
- S3 Bucket: {bucket_name}
- Report Location: {report_key}
- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

You can view the report by downloading it from S3:
aws s3 cp s3://{bucket_name}/{report_key} ./report.html

This is an automated message from the CloudWatch Reporting System.
            """
        else:
            subject = "CloudWatch Report Generation Failed"
            message = f"""
CloudWatch CPU Utilization Report generation has failed.

Error Details:
- Error: {error}
- Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

Please check the CloudWatch Logs for more detailed error information.

This is an automated message from the CloudWatch Reporting System.
            """

        response = sns_client.publish(
            TopicArn=sns_topic_arn,
            Subject=subject,
            Message=message
        )

        logger.info("Sent SNS notification", extra={
            "message_id": response['MessageId'],
            "success": success
        })

    except Exception as e:
        logger.error("Error sending SNS notification", extra={
            "error": str(e),
            "success": success
        })
