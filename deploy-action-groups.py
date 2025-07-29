#!/usr/bin/env python3

import argparse
from botocore.exceptions import ClientError
import boto3
import json
import logging
import os
import sys
import time
import uuid
import zipfile



# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('ActionGroupDeployer')


def zip_lambda_file(lambda_path, lambda_function_name):
    """
    Creates zip file from lambda file.
    
    Args:
        lambda_file_path (str): Path to the lambda file
        
    Returns:
        str: Path to the created zip file
    """   
    # Ensure build directory exists
    build_dir = f"{lambda_path}/build"
    os.makedirs(build_dir, exist_ok=True)
    
    # Create zip file path
    zip_path = f"{build_dir}/{lambda_function_name}.zip"
    
    # Lambda source directory path
    lambda_source_dir = f"{lambda_path}/source"
    
    # Check if source directory exists
    if not os.path.exists(lambda_source_dir):
        raise FileNotFoundError(f"Lambda source directory not found: {lambda_source_dir}")
    
    # Create zip file
    with zipfile.ZipFile(zip_path, 'w') as z:
        for root, dirs, files in os.walk(lambda_source_dir):
            for file in files:
                file_path = os.path.join(root, file)
                z.write(file_path, file)
        
    logger.info(f"Created zip file: {zip_path}")
    return zip_path


def create_execution_role(lambda_role_name):
    """
    Creates a Lambda execution role with basic execution permissions.

    Args:
        lambda_role_name (str): Name of the Lambda execution role

    Returns:
        str: ARN of the created role
    """
    try:
        iam_client = boto3.client('iam')
        
        # Create the role
        response = iam_client.create_role(
            RoleName=lambda_role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }]
            })
        )
        role_arn = response['Role']['Arn']
        
        # Attach basic Lambda execution policy
        iam_client.attach_role_policy(
            RoleName=lambda_role_name,
            PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
        )
        
        logger.info(f"Lambda execution role created: {role_arn}")
        return role_arn
    except Exception as e:
        logger.error(f"Failed to create Lambda execution role: {e}")
        return False
    

def add_bedrock_permission(lambda_name, region):
    """
    Add permission for Bedrock service to invoke the Lambda function.
    
    Args:
        lambda_name (str): Name of the Lambda function
        region (str): Region for the Lambda and resources to be created in
        
    Returns:
        bool: True if permission was added successfully, False otherwise
    """
    try:
        lambda_client = boto3.client('lambda', region_name=region)
        lambda_client.add_permission(
            FunctionName=lambda_name,
            StatementId='bedrock-invoke-permission',
            Action='lambda:InvokeFunction',
            Principal='bedrock.amazonaws.com'
        )
        logger.info(f"Added Bedrock invoke permission to {lambda_name}")
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceConflictException':
            logger.info(f"Bedrock permission already exists for {lambda_name}")
            return True
        else:
            logger.error(f"Failed to add Bedrock permission to {lambda_name}: {e}")
            return False
    except Exception as e:
        logger.error(f"Failed to add Bedrock permission to {lambda_name}: {e}")
        return False
    

def create_lambda(zip_file_path, lambda_name, role_arn, region):
    """
    Creates Lambda function from zip file in local machine.
    
    Args:
        zip_file_path (str): Path to the zip file of Lambda code
        lambda_name (str): Name of the Lambda function
        role_arn (str): ARN of the Lambda execution role
        region (str): Region for the Lambda and resources to be created in
        
    Returns:
        str: ARN of the created Lambda function, or None if failed
    """
    try:
        lambda_client = boto3.client('lambda', region_name=region)

        with open(zip_file_path, 'rb') as file_data:
            bytes_content = file_data.read()

        response = lambda_client.create_function(
            FunctionName=lambda_name,
            Runtime='python3.12',
            Role=role_arn,
            Handler="agent-query-history.lambda_handler",
            Code={'ZipFile': bytes_content},
            Timeout=60,
            MemorySize=128,
            Publish=True
        )
        logger.info(f"Lambda function created: {lambda_name}")
        return response['FunctionArn']
    except Exception as e:
        logger.error(f"Failed to create Lambda function: {e}")
        return False


def deploy_lambda(lambda_path, lambda_function_name, lambda_role_name, region):
    """
    Deploy a Lambda function from a Python file.
    
    Args:
        lambda_file_path (str): Path to the Lambda Python file
        lambda_function_name (str): Lambda function name to deploy
        role_arn (str): ARN of the Lambda execution role
        region (str): Region for the Lambda and resources to be created in
        
    Returns:
        dict: Information about the deployed Lambda function
    """
    # Create zip file
    zip_path = zip_lambda_file(lambda_path, lambda_function_name)

    # Create Lambda execution role
    role_arn = create_execution_role(lambda_role_name)
    if not role_arn:
        return {
            "name": lambda_function_name, 
            "status": "failed", 
            "lambda_arn": None,
            "exec_role_arn": None,
            "error": "Failed to create execution role",
            }
    
    # Wait for IAM role to propagate through AWS services
    wait_seconds = int(os.environ.get('IAM_PROPAGATION_WAIT', 10))
    logger.info(f"Waiting {wait_seconds}s for IAM role propagation")
    time.sleep(wait_seconds)

    # Create Lambda function
    lambda_arn = create_lambda(zip_path, lambda_function_name, role_arn, region)
    if not lambda_arn:
        return {
            "name": lambda_function_name, 
            "status": "failed", 
            "lambda_arn": None,
            "exec_role_arn": role_arn,
            "error": "Failed to create Lambda function",
            }
    
    # Add Bedrock permission to invoke the Lambda function
    if not add_bedrock_permission(lambda_function_name, region):
        logger.warning(f"Failed to add Bedrock permission to {lambda_function_name}, but continuing...")
    
    return {
        "name": lambda_function_name,
        "status": "success",
        "lambda_arn": lambda_arn,
        "exec_role_arn": role_arn,
        "error": None
    }


def get_param(arg_value, env_var_name, default_value):
    """
    Get parameter value with priority: 1) command-line arg, 2) environment variable, 3) default value
    
    Args:
        arg_value: Value from command-line argument
        env_var_name (str): Name of environment variable to check
        default_value: Default value to use if neither arg nor env var is set
        
    Returns:
        Value from the highest priority source available
    """
    if arg_value is not None:
        return arg_value
    
    env_value = os.environ.get(env_var_name)
    if env_value is not None:
        return env_value
    
    return default_value


def main():
    parser = argparse.ArgumentParser(description="Deploy agent Lambda functions")
    parser.add_argument("--region", help="AWS region, can also read from AWS_REGION env")
    parser.add_argument("--prefix", help="Project Prefix for resources created, can also read from PROJECT_PREFIX env")
    parser.add_argument("--suffix", help="Project Suffix (4 character) to gurantee resource uniqueness, can also read from PROJECT_SUFFIX env")
    args = parser.parse_args()
    
    # Get configuration using priority: args > env vars > defaults
    region = get_param(args.region, "AWS_REGION", "us-east-1")
    prefix = get_param(args.prefix, "PROJECT_PREFIX", "sample-asa")
    suffix = get_param(args.suffix, "PROJECT_SUFFIX", str(uuid.uuid4())[:4])
       
    # Create build package directory if it doesn't exist
    lambda_working_directory = "./agent-action-groups"
    os.makedirs(f"{lambda_working_directory}/build", exist_ok=True)

    # Prepare deployment parameters
    lambda_function_name = f"{prefix}-agent-query-history-{suffix}"
    lambda_role_name = f"{prefix}-agent-query-history-role-{suffix}"

    logger.info(f"Deploying Lambda function: {lambda_function_name}")

    exit_code = 0

    # deploy lambda for action group
    try: 
        results = deploy_lambda(
            lambda_working_directory,
            lambda_function_name,
            lambda_role_name,
            region
        )
        if results.get("status") == "failed":
            logger.error(f"Deployment failed: {results.get('error')}")
            exit_code = 1
        else:
            logger.info(f"Deployment results: {json.dumps(results, indent=2)}")
    except Exception as e:
        logger.error(f"Failed to deploy Lambda function: {lambda_function_name}: {e}")
        exit_code = 1
    
    # return final creation results
    return exit_code

if __name__ == "__main__":
    sys.exit(main())