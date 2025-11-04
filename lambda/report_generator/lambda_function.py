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
    Lambda handler for Report Generator using AWS built-in layers
    """
    logger.info("Report Generator Lambda started", extra={"event": event})
    
    try:
        # Extract S3 information from the event or environment
        bucket_name = event.get('bucket_name') or os.environ.get('S3_BUCKET_NAME')
        data_key = event.get('s3_key')
        
        if not bucket_name or not data_key:
            raise ValueError("Missing required parameters: bucket_name or s3_key")
        
        logger.info("Processing report generation", extra={
            "bucket_name": bucket_name,
            "data_key": data_key
        })
        
        # Generate the report
        report_key = generate_pandas_report(bucket_name, data_key)
        
        # Send success notification
        send_notification(bucket_name, report_key, success=True)
        
        logger.info("Report generation completed successfully", extra={
            "report_key": report_key
        })
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Report generated successfully',
                'report_key': report_key
            })
        }
        
    except Exception as e:
        logger.error("Report generation failed", extra={"error": str(e)})
        
        # Send failure notification
        send_notification(None, None, success=False, error=str(e))
        
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'Report generation failed',
                'error': str(e)
            })
        }

def generate_pandas_report(bucket_name, data_key):
    """
    Generate HTML report using pandas for data processing
    """
    # Download metrics data from S3
    metrics_data = download_metrics_data(bucket_name, data_key)
    
    # Process data with pandas
    df = process_metrics_with_pandas(metrics_data)
    
    # Create HTML report
    html_path = create_html_report(df, metrics_data)
    
    # Upload report to S3
    report_key = upload_report_to_s3(bucket_name, html_path, metrics_data['month'])
    
    return report_key

def download_metrics_data(bucket_name, data_key):
    """
    Download and parse metrics data from S3
    """
    logger.info("Downloading metrics data from S3", extra={
        "bucket": bucket_name,
        "key": data_key
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
            "bucket": bucket_name,
            "key": data_key,
            "error": str(e)
        })
        raise

def process_metrics_with_pandas(metrics_data):
    """
    Process metrics data using pandas for analysis
    """
    logger.info("Processing metrics data with pandas")
    
    # Convert metrics data to pandas DataFrame
    rows = []
    for instance_data in metrics_data.get('instances', []):
        instance_id = instance_data['instance_id']
        cpu_data = instance_data.get('cpu_data', [])
        
        for datapoint in cpu_data:
            rows.append({
                'instance_id': instance_id,
                'date': datapoint['timestamp'],
                'avg_cpu': datapoint['average'],
                'max_cpu': datapoint['maximum'],
                'min_cpu': datapoint['minimum']
            })
    
    if not rows:
        # Create empty DataFrame with expected columns
        df = pd.DataFrame(columns=['instance_id', 'date', 'avg_cpu', 'max_cpu', 'min_cpu'])
    else:
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'])
    
    logger.info("Successfully processed data with pandas", extra={
        "rows": len(df),
        "columns": list(df.columns)
    })
    
    return df

def create_html_report(df, metrics_data):
    """
    Create HTML report using pandas styling
    """
    logger.info("Creating HTML report")
    
    try:
        # Create temporary HTML file
        html_path = tempfile.mktemp(suffix='.html')
        
        month = metrics_data.get('month', 'Unknown Month')
        
        # Start HTML content
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>CloudWatch CPU Utilization Report - {month}</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; }}
                h1 {{ color: #232F3E; text-align: center; }}
                h2 {{ color: #FF9900; border-bottom: 2px solid #FF9900; }}
                .summary {{ background-color: #f8f9fa; padding: 20px; border-radius: 5px; margin: 20px 0; }}
                table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
                th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
                th {{ background-color: #232F3E; color: white; }}
                tr:nth-child(even) {{ background-color: #f2f2f2; }}
                .metric {{ display: inline-block; margin: 10px 20px; }}
                .high {{ color: #d32f2f; font-weight: bold; }}
                .normal {{ color: #388e3c; }}
                .low {{ color: #1976d2; }}
            </style>
        </head>
        <body>
            <h1>CloudWatch CPU Utilization Report</h1>
            <h2>Report Period: {month}</h2>
        """
        
        # Add summary statistics
        if not df.empty:
            total_instances = df['instance_id'].nunique()
            avg_cpu_overall = df['avg_cpu'].mean()
            max_cpu_overall = df['max_cpu'].max()
            min_cpu_overall = df['min_cpu'].min()
            
            html_content += f"""
            <div class="summary">
                <h2>Executive Summary</h2>
                <div class="metric">Total Instances: <strong>{total_instances}</strong></div>
                <div class="metric">Overall Average CPU: <strong>{avg_cpu_overall:.2f}%</strong></div>
                <div class="metric">Peak CPU Usage: <strong>{max_cpu_overall:.2f}%</strong></div>
                <div class="metric">Minimum CPU Usage: <strong>{min_cpu_overall:.2f}%</strong></div>
            </div>
            """
            
            # Add instance summary table
            instance_summary = df.groupby('instance_id').agg({
                'avg_cpu': ['mean', 'std'],
                'max_cpu': 'max',
                'min_cpu': 'min',
                'date': 'count'
            }).round(2)
            
            instance_summary.columns = ['Avg CPU', 'CPU StdDev', 'Max CPU', 'Min CPU', 'Data Points']
            instance_summary = instance_summary.reset_index()
            
            html_content += "<h2>Instance Summary</h2>"
            html_content += instance_summary.to_html(classes='summary-table', escape=False, index=False)
            
            # Add detailed daily data
            html_content += "<h2>Daily CPU Utilization Details</h2>"
            
            # Format the detailed data
            df_display = df.copy()
            df_display['avg_cpu'] = df_display['avg_cpu'].round(2)
            df_display['max_cpu'] = df_display['max_cpu'].round(2)
            df_display['min_cpu'] = df_display['min_cpu'].round(2)
            df_display['date'] = df_display['date'].dt.strftime('%Y-%m-%d')
            
            # Rename columns for display
            df_display.columns = ['Instance ID', 'Date', 'Average CPU (%)', 'Maximum CPU (%)', 'Minimum CPU (%)']
            
            html_content += df_display.to_html(classes='detail-table', escape=False, index=False)
            
            # Add recommendations
            html_content += "<h2>Recommendations</h2><ul>"
            
            for instance_id in df['instance_id'].unique():
                instance_df = df[df['instance_id'] == instance_id]
                avg_cpu = instance_df['avg_cpu'].mean()
                max_cpu = instance_df['max_cpu'].max()
                
                recommendation = generate_recommendations(avg_cpu, max_cpu)
                html_content += f"<li><strong>{instance_id}:</strong> {recommendation}</li>"
            
            html_content += "</ul>"
            
        else:
            html_content += """
            <div class="summary">
                <h2>No Data Available</h2>
                <p>No CPU utilization data was found for the specified time period.</p>
            </div>
            """
        
        # Close HTML
        html_content += f"""
            <div style="margin-top: 40px; text-align: center; color: #666; font-size: 12px;">
                Report generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')} by CloudWatch S3 Reporting System
            </div>
        </body>
        </html>
        """
        
        # Write HTML file
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        logger.info("Successfully created HTML report", extra={"html_path": html_path})
        return html_path
        
    except Exception as e:
        logger.error("Error creating HTML report", extra={"error": str(e)})
        raise

def generate_recommendations(avg_cpu, max_cpu):
    """
    Generate basic recommendations based on CPU usage patterns
    """
    if avg_cpu < 20:
        return "CPU utilization is low. Consider downsizing the instance type to reduce costs."
    elif avg_cpu > 80:
        return "CPU utilization is high. Consider upgrading to a larger instance type or implementing auto-scaling."
    elif max_cpu > 95:
        return "CPU spikes detected. Monitor for performance issues and consider implementing auto-scaling."
    else:
        return "CPU utilization appears to be within normal ranges."

def upload_report_to_s3(bucket_name, html_path, month):
    """
    Upload HTML report to S3 reports folder
    """
    logger.info("Uploading HTML report to S3")
    
    try:
        # Create S3 key for the report
        report_key = f"reports/{month}-report.html"
        
        # Upload HTML to S3
        with open(html_path, 'rb') as html_file:
            s3_client.put_object(
                Bucket=bucket_name,
                Key=report_key,
                Body=html_file,
                ContentType='text/html'
            )
        
        logger.info("Successfully uploaded HTML report to S3", extra={
            "bucket": bucket_name,
            "key": report_key
        })
        
        return report_key
        
    except Exception as e:
        logger.error("Error uploading HTML report to S3", extra={
            "bucket": bucket_name,
            "error": str(e)
        })
        raise
    finally:
        # Clean up HTML file
        if os.path.exists(html_path):
            os.remove(html_path)

def send_notification(bucket_name, report_key, success=True, error=None):
    """
    Send SNS notification about report status
    """
    try:
        sns_topic_arn = os.environ.get('SNS_TOPIC_ARN')
        if not sns_topic_arn:
            logger.warning("SNS_TOPIC_ARN environment variable not set, skipping notification")
            return
        
        if success:
            subject = "CloudWatch Report Generated Successfully"
            message = f"""
CloudWatch CPU Utilization Report has been generated successfully.

Report Details:
- S3 Bucket: {bucket_name}
- Report Location: {report_key}
- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

You can view the report by downloading it from S3 or using the following AWS CLI command:
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
        
        # Send SNS notification
        response = sns_client.publish(
            TopicArn=sns_topic_arn,
            Subject=subject,
            Message=message
        )
        
        logger.info("Successfully sent SNS notification", extra={
            "message_id": response['MessageId'],
            "success": success
        })
        
    except Exception as e:
        logger.error("Error sending SNS notification", extra={
            "error": str(e),
            "success": success
        })