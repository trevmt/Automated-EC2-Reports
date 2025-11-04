#!/usr/bin/env python3
"""
SAM deployment script for CloudWatch S3 Reporting
"""

import subprocess
import sys
from pathlib import Path

def run_command(command, description):
    """Run a command and handle errors"""
    print(f"\n[INFO] {description}...")
    print(f"Command: {' '.join(command)}")
    
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        print(f"[SUCCESS] {description} completed successfully")
        if result.stdout:
            print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] {description} failed")
        print(f"Error: {e}")
        if e.stdout:
            print(f"stdout: {e.stdout}")
        if e.stderr:
            print(f"stderr: {e.stderr}")
        return False

def check_sam_cli():
    """Check if SAM CLI is installed"""
    try:
        result = subprocess.run(["sam", "--version"], capture_output=True, text=True)
        print(f"[SUCCESS] SAM CLI found: {result.stdout.strip()}")
        return True
    except FileNotFoundError:
        print("[ERROR] SAM CLI not found. Please install it:")
        print("https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html")
        return False

def main():
    print("CloudWatch S3 Reporting - SAM Deployment")
    print("=" * 50)
    
    # Check prerequisites
    if not check_sam_cli():
        sys.exit(1)
    
    # Check for samconfig.toml
    samconfig_path = Path("samconfig.toml")
    if not samconfig_path.exists():
        print("[WARNING] samconfig.toml not found")
        print("Please copy samconfig.toml.example to samconfig.toml and configure it first:")
        print("  cp samconfig.toml.example samconfig.toml")
        print("Then edit samconfig.toml with your specific values")
        sys.exit(1)
    
    # Validate template
    if not run_command(["sam", "validate"], "Validating SAM template"):
        sys.exit(1)
    
    # Build application
    if not run_command(["sam", "build"], "Building SAM application"):
        sys.exit(1)
    
    # Deploy application
    print("\n[INFO] Deploying SAM application...")
    print("Using configuration from samconfig.toml")
    
    deploy_command = ["sam", "deploy"]
    
    try:
        subprocess.run(deploy_command, check=True)
        print("[SUCCESS] Deployment completed successfully!")
        
        print("\n" + "=" * 50)
        print("DEPLOYMENT COMPLETE!")
        print("=" * 50)
        print("\nNext steps:")
        print("1. Check your email and confirm the SNS subscription")
        print("2. Wait for the first scheduled run (every 5 minutes in demo mode)")
        print("3. Check S3 bucket for generated reports")
        print("\nUseful commands:")
        print("- View logs: sam logs -n DataExporterFunction --tail")
        print("- Test locally: sam local invoke ReportGeneratorFunction -e events/test-report-generator.json")
        print("- Update stack: sam deploy")
        print("- Guided deployment: sam deploy --guided")
        
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Deployment failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()