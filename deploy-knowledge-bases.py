#!/usr/bin/env python3

import argparse
from botocore.exceptions import ClientError
import boto3
import json
import logging
from opensearchpy import OpenSearch, RequestsHttpConnection
import os
import requests
from requests_aws4auth import AWS4Auth
import sys
import time
import uuid

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('KnowledgeBaseDeployer')

class KnowledgeBaseCreator:
    def __init__(self, region, prefix, suffix):
        self.session = boto3.Session()
        
        # Get configuration 
        self.account_id = self.session.client('sts').get_caller_identity()['Account']
        self.region = region
        self.prefix = prefix
        self.suffix = suffix or str(uuid.uuid4())[:4]  # Generate a random 4-character string if suffix is not provided
        self.role_arn = None

        # Initialize AWS clients
        self.opensearch_client = self.session.client('opensearchserverless', region_name=self.region)
        self.bedrock_client = self.session.client('bedrock-agent', region_name=self.region)
        self.s3_client = self.session.client('s3', region_name=self.region)
        self.iam_client = self.session.client('iam', region_name=self.region)

        try:
            # Get the current caller identity
            sts_client = self.session.client('sts')
            caller_identity = sts_client.get_caller_identity()
            caller_arn = caller_identity['Arn']
            
            # If it's a user ARN, use it directly
            if ':user/' in caller_arn:
                self.user_arn = caller_arn
                logger.info(f"Using current user ARN: {self.user_arn}")
            # If it's an assumed role, extract the role ARN
            elif ':assumed-role/' in caller_arn:
                # Extract role name from assumed-role ARN
                role_name, identity_name = caller_arn.split('/')[-2:]
                self.user_arn = f"arn:aws:sts::{self.account_id}:assumed-role/{role_name}/{identity_name}"
                logger.info(f"Using assumed role ARN: {self.user_arn}")
            else:
                raise RuntimeError(f"cannot identify deployer identity")
        except Exception as e:
            raise type(e)(f"Failed to discover caller identity: {str(e)}")
        
        logger.info(f"Using AWS Account: {self.account_id}")
        logger.info(f"Using Region: {self.region}")


    def _wait_with_exponential_backoff(self, check_function, max_attempts=30, base_delay=2, max_delay=60, description="resource"):
        """Wait for a condition with exponential backoff"""
        for attempt in range(max_attempts):
            try:
                if check_function():
                    return True
                
                # Calculate delay with exponential backoff and jitter
                delay = min(base_delay * (2 ** attempt), max_delay)
                logger.info(f"Waiting for {description}... (attempt {attempt + 1}/{max_attempts}, next check in {delay:.1f}s)")
                time.sleep(delay)
                
            except Exception as e:
                logger.warning(f"Error checking {description} status: {str(e)}")
                delay = min(base_delay * (2 ** attempt), max_delay)
                time.sleep(delay)
        
        logger.error(f"❌ {description} did not become ready within timeout ({max_attempts} attempts)")
        return False


    def _validate_opensearch_permissions(self, collection_id):
        """Validate that the current user has permissions to access the OpenSearch collection"""
        try:
            collection_endpoint = f"https://{collection_id}.{self.region}.aoss.amazonaws.com"
            credentials = self.session.get_credentials()
            auth = AWS4Auth(credentials.access_key, credentials.secret_key, self.region, 'aoss', session_token=credentials.token)
            
            logger.info(f"Validating OpenSearch permissions for {collection_endpoint}")

            # Try a simple health check to validate permissions
            response = requests.get(f"{collection_endpoint}/_cluster/health", auth=auth, timeout=10)
            
            if response.status_code == 200:
                logger.info("OpenSearch permissions validated successfully")
                return True
            elif response.status_code == 403:
                logger.error("OpenSearch permissions validation failed - Access Denied")
                logger.error(f"Current user ARN: {self.user_arn}")
                logger.error(f"Agent role ARN: {self.role_arn}")
                logger.error("The data access policy may not include the correct principal ARNs")
                return False
            else:
                logger.warning(f"OpenSearch permissions validation returned status: {response.status_code}")
                return True  # Proceed anyway
                
        except Exception as e:
            logger.warning(f"Could not validate OpenSearch permissions: {str(e)}")
            return True  # Proceed anyway


    def create_s3_bucket(self, bucket_name):
        """Create S3 bucket"""
        try:
            if self.region == 'us-east-1':
                self.s3_client.create_bucket(Bucket=bucket_name)
            else:
                self.s3_client.create_bucket(
                    Bucket=bucket_name,
                    CreateBucketConfiguration={'LocationConstraint': self.region}
                )
            
            logger.info(f"Created S3 bucket: {bucket_name}")
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'BucketAlreadyOwnedByYou':
                logger.info(f"S3 bucket already exists: {bucket_name}")
                return True
            else:
                logger.error(f"Failed to create S3 bucket: {e}")
                return False


    def upload_s3_data(self, local_path, s3_bucket, s3_prefix):
        """Upload local data to S3"""
        try:
            logger.info(f"Uploading {local_path} data to S3")
            
            # Upload files
            uploaded_count = 0
            for root, dirs, files in os.walk(local_path):
                for file in files:
                    local_file_path = os.path.join(root, file)
                    relative_path = os.path.relpath(local_file_path, local_path)
                    s3_key = f"{s3_prefix}/{relative_path}"
                    
                    logger.info(f"Uploading {local_file_path} to s3://{s3_bucket}/{s3_key}")
                    self.s3_client.upload_file(local_file_path, s3_bucket, s3_key)
                    uploaded_count += 1
            
            logger.info(f"Uploaded {uploaded_count} files to s3://{s3_bucket}/{s3_prefix}/")
            return True
            
        except Exception as e:
            logger.error(f"Failed to upload S3 data: {str(e)}")
            return False


    def create_kb_role(self, role_name, collection_arn, s3_bucket_name):
        """Create IAM role for Bedrock Knowledge Base"""
        try:            
            # Trust policy for Bedrock
            trust_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AmazonBedrockKnowledgeBaseTrustPolicy",
                        "Effect": "Allow",
                        "Principal": {
                            "Service": "bedrock.amazonaws.com"
                        },
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "StringEquals": {
                                "aws:SourceAccount": f"{self.account_id}"
                            },
                            "ArnLike": {
                                "aws:SourceArn": f"arn:aws:bedrock:{self.region}:{self.account_id}:knowledge-base/*"
                            }
                        }
                    }
                ]
            }
            
            # Create role
            response = self.iam_client.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy)
            )
            
            # Attach policies for S3 and OpenSearch access
            policies = [
                {
                    "policy_name": f"BedrockTitanV2Policy-{self.suffix}",
                    "policy": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "BedrockInvokeModelStatement",
                                "Effect": "Allow",
                                "Action": [
                                    "bedrock:InvokeModel"
                                ],
                                "Resource": [
                                    f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0"
                                ]
                            }
                        ]
                    }
                },
                {
                    "policy_name": f"BedrockOssPolicy-{self.suffix}",
                    "policy": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "OpenSearchServerlessAPIAccessAllStatement",
                                "Effect": "Allow",
                                "Action": [
                                    "aoss:APIAccessAll"
                                ],
                                "Resource": [
                                    collection_arn
                                ]
                            }
                        ]
                    }
                },
                {
                    "policy_name": f"BedrockS3Policy-{self.suffix}",
                    "policy": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "S3ListBucketStatement",
                                "Effect": "Allow",
                                "Action": [
                                    "s3:ListBucket"
                                ],
                                "Resource": [
                                    f"arn:aws:s3:::{s3_bucket_name}"
                                ],
                                "Condition": {
                                    "StringEquals": {
                                        "aws:ResourceAccount": [
                                            self.account_id
                                        ]
                                    }
                                }
                            },
                            {
                                "Sid": "S3GetObjectStatement",
                                "Effect": "Allow",
                                "Action": [
                                    "s3:GetObject"
                                ],
                                "Resource": [
                                    f"arn:aws:s3:::{s3_bucket_name}/manuals-kb/*"
                                ],
                                "Condition": {
                                    "StringEquals": {
                                        "aws:ResourceAccount": [
                                            self.account_id
                                        ]
                                    }
                                }
                            }
                        ]
                    }
                }
            ]
            
            for policy_block in policies:
                self.iam_client.put_role_policy(
                    RoleName=role_name,
                    PolicyName=policy_block["policy_name"],
                    PolicyDocument=json.dumps(policy_block["policy"])
                )

            # Wait for role to propagate
            logger.info(f"Waiting for kb iam role to propagate...")
            wait_seconds = int(os.environ.get('ROLE_PROPAGATION_WAIT', 15))
            logger.info(f"Waiting {wait_seconds}s for role propagation")
            time.sleep(wait_seconds)

            logger.info(f"Created KB role: {self.role_arn}")

            return True
            
        except ClientError as e:
            logger.error(f"Failed to create KB role: {e}")
            return False


    def create_encryption_policy(self, policy_name, collection_name):
        """Create encryption policy if it doesn't exist"""
        try:           
            # Create new policy
            policy = {
                "Rules": [
                    {
                        "ResourceType": "collection",
                        "Resource": [f"collection/{collection_name}"]
                    }
                ],
                "AWSOwnedKey": True
            }
            
            self.opensearch_client.create_security_policy(
                name=policy_name,
                type='encryption',
                policy=json.dumps(policy)
            )
            logger.info(f"Created encryption policy: {policy_name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create encryption policy: {str(e)}")
            return False


    def create_network_policy(self, policy_name, collection_name):
        """Create network policy if it doesn't exist"""
        try:          
            # Create new policy
            policy = [
                {
                    "Rules": [
                        {
                            "ResourceType": "collection",
                            "Resource": [f"collection/{collection_name}"]
                        },
                        {
                            "ResourceType": "dashboard",
                            "Resource": [f"collection/{collection_name}"]
                        }
                    ],
                    "AllowFromPublic": True
                }
            ]
            
            self.opensearch_client.create_security_policy(
                name=policy_name,
                type='network',
                policy=json.dumps(policy)
            )
            logger.info(f"Created network policy: {policy_name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create network policy: {str(e)}")
            return False


    def create_data_access_policy(self, policy_name, collection_name):
        """Create data access policy if it doesn't exist"""
        try:
            # Create new policy
            policy = [
                {
                    "Rules": [
                        {
                            "Resource": [f"collection/{collection_name}"],
                            "Permission": [
                                "aoss:CreateCollectionItems",
                                "aoss:DeleteCollectionItems", 
                                "aoss:UpdateCollectionItems",
                                "aoss:DescribeCollectionItems"
                            ],
                            "ResourceType": "collection"
                        },
                        {
                            "Resource": [f"index/{collection_name}/*"],
                            "Permission": [
                                "aoss:CreateIndex",
                                "aoss:DeleteIndex",
                                "aoss:UpdateIndex", 
                                "aoss:DescribeIndex",
                                "aoss:ReadDocument",
                                "aoss:WriteDocument"
                            ],
                            "ResourceType": "index"
                        }
                    ],
                    "Principal": [self.role_arn, self.user_arn]
                }
            ]
            
            logger.info(f"Creating data access policy with principals: {[self.role_arn, self.user_arn]}")
            
            self.opensearch_client.create_access_policy(
                name=policy_name,
                type='data',
                policy=json.dumps(policy)
            )
            logger.info(f"Created data access policy: {policy_name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create data access policy: {str(e)}")
            return False


    def create_collection(self, collection_name):
        """Create OpenSearch collection"""
        try:            
            # Create new collection
            response = self.opensearch_client.create_collection(
                name=collection_name,
                type='VECTORSEARCH'
            )
            
            collection_id = response['createCollectionDetail']['id']
            logger.info(f"Collection creation initiated with id {collection_id}. Waiting for it to become active...")
            
            return collection_id            
        except Exception as e:
            logger.error(f"Failed to create collection: {str(e)}")
            return None


    def create_vector_index(self, collection_id, index_name):
        """Create vector index with correct configuration"""
        try:
            collection_host = f"{collection_id}.{self.region}.aoss.amazonaws.com"
            
            # Get credentials
            credentials = self.session.get_credentials()
            
            # Create AWS4Auth
            auth = AWS4Auth(
                credentials.access_key,
                credentials.secret_key,
                self.region,
                'aoss',
                session_token=credentials.token
            )

            oss_client = OpenSearch(
                hosts=[{'host': collection_host, 'port': 443}],
                http_auth=auth,
                use_ssl=True,
                verify_certs=True,
                connection_class=RequestsHttpConnection,
                timeout=300
            )
            
            # FIXED: Use l2 space type instead of cosinesimil with faiss engine
            index_config = {
                "settings": {
                    "index": {
                        "knn": True
                    }
                },
                "mappings": {
                    "properties": {
                        "bedrock-knowledge-base-default-vector": {
                            "type": "knn_vector",
                            "dimension": 1024,
                            "method": {
                                "name": "hnsw",
                                "space_type": "l2",  # FIXED: Changed from cosinesimil to l2
                                "engine": "faiss"
                            }
                        },
                        "AMAZON_BEDROCK_TEXT_CHUNK": {
                            "type": "text"
                        },
                        "AMAZON_BEDROCK_METADATA": {
                            "type": "text"
                        }
                    }
                }
            }

            response = oss_client.indices.create(index=index_name, body=index_config)
                        
            if response.get("acknowledged"):
                logger.info(f"Successfully created index: {index_name}")
                return True
            else:
                logger.error(f"Failed to create vector index: {response}")
                return False
                
        except Exception as e:
            logger.error(f"Exception creating vector index: {str(e)}")
            return False


    def create_knowledge_base(self, kb_name, collection_arn, index_name, s3_bucket, s3_prefix):
        """Create Bedrock knowledge base"""
        try:                        
            kb_config = {
                'name': kb_name,
                'description': f'Knowledge base for {kb_name}',
                'roleArn': self.role_arn,
                'knowledgeBaseConfiguration': {
                    'type': 'VECTOR',
                    'vectorKnowledgeBaseConfiguration': {
                        'embeddingModelArn': f'arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0'
                    }
                },
                'storageConfiguration': {
                    'type': 'OPENSEARCH_SERVERLESS',
                    'opensearchServerlessConfiguration': {
                        'collectionArn': collection_arn,
                        'vectorIndexName': index_name,
                        'fieldMapping': {
                            'vectorField': 'bedrock-knowledge-base-default-vector',
                            'textField': 'AMAZON_BEDROCK_TEXT_CHUNK',
                            'metadataField': 'AMAZON_BEDROCK_METADATA'
                        }
                    }
                }
            }
            
            logger.info(f"Creating knowledge base: {kb_name}, using configs {kb_config}")
            response = self.bedrock_client.create_knowledge_base(**kb_config)
            
            kb_id = response['knowledgeBase']['knowledgeBaseId']
            logger.info(f"Created knowledge base: {kb_id}")
            
            # Wait for knowledge base to become active
            logger.info("Waiting for knowledge base to become active...")

            max_attempts = 30
            for attempt in range(max_attempts):
                try:
                    kb_response = self.bedrock_client.get_knowledge_base(knowledgeBaseId=kb_id)
                    status = kb_response['knowledgeBase']['status']
                    
                    if status == 'ACTIVE':
                        logger.info(f"Knowledge base {kb_id} is now active")
                        break
                    elif status == 'FAILED':
                        logger.error(f"Knowledge base {kb_id} failed to create")
                        return None, None, None
                    
                    logger.info(f"Knowledge base status: {status} (attempt {attempt + 1}/{max_attempts})")
                    sleep_time = attempt * attempt + 1
                    logger.debug(f"Waiting {sleep_time:.2f} seconds before next attempt")
                    time.sleep(sleep_time)
                    
                except Exception as e:
                    logger.warning(f"Error checking knowledge base status: {str(e)}")
                    sleep_time = attempt * attempt + 1
                    logger.debug(f"Waiting {sleep_time:.2f} seconds before next attempt")
                    time.sleep(sleep_time)
            
            # Create data source
            ds_config = {
                'knowledgeBaseId': kb_id,
                'name': f'{kb_name}-datasource',
                'description': f'Data source for {kb_name}',
                'dataSourceConfiguration': {
                    'type': 'S3',
                    's3Configuration': {
                        'bucketArn': f'arn:aws:s3:::{s3_bucket}',
                        'inclusionPrefixes': [s3_prefix + '/']
                    }
                }
            }
            
            logger.info(f"Creating data source for knowledge base: {kb_id}")
            ds_response = self.bedrock_client.create_data_source(**ds_config)
            
            ds_id = ds_response['dataSource']['dataSourceId']
            logger.info(f"Created data source: {ds_id}")
            
            # Start ingestion job
            logger.info(f"Starting ingestion job for data source: {ds_id}")
            ingestion_response = self.bedrock_client.start_ingestion_job(
                knowledgeBaseId=kb_id,
                dataSourceId=ds_id
            )
            
            job_id = ingestion_response['ingestionJob']['ingestionJobId']
            logger.info(f"Started ingestion job: {job_id}")
            
            return kb_id, ds_id, job_id
            
        except Exception as e:
            logger.error(f"Failed to create knowledge base: {str(e)}")
            return None, None, None


    def create_knowledge_base_complete(self, kb_name, role_name, collection_arn, index_name, local_data_path, s3_prefix):
        
        """Create complete knowledge base with all components"""
        logger.info(f"Ingesting Data...")
        # Create S3 bucket
        s3_bucket_name = f"{self.prefix}-kb-source-{self.account_id}-{self.region}-{self.suffix}"
        if not self.create_s3_bucket(s3_bucket_name):
            return False
        
        # Upload S3 data
        if not self.upload_s3_data(local_data_path, s3_bucket_name, s3_prefix):
            return False

        if not self.create_kb_role(role_name, self.collection_arn, s3_bucket_name):
            return False
        
        # Create knowledge base
        kb_id, ds_id, job_id = self.create_knowledge_base(kb_name, collection_arn, index_name, s3_bucket_name, s3_prefix)
        
        if kb_id:
            return {
                'success': True,
                'knowledge_base_id': kb_id,
                'collection_arn': collection_arn,
                'index_name': index_name,
                'data_source_id': ds_id,
                'ingestion_job_id': job_id
            }
        else:
            return False


    def create_vector_db(self, collection_name, index_name):
        """Create vector DB and its needed components"""
        logger.info("Creating OpenSearch policies...")

        encryption_policy_name = f"{self.prefix}-encryption-policy-{self.suffix}"
        network_policy_name = f"{self.prefix}-network-policy-{self.suffix}"
        access_policy_name = f"{self.prefix}-data-policy-{self.suffix}"

        if not self.create_encryption_policy(encryption_policy_name, collection_name):
            return False
            
        if not self.create_network_policy(network_policy_name, collection_name):
            return False
            
        if not self.create_data_access_policy(access_policy_name, collection_name):
            return False
        
        # Wait for policies to propagate - policies need time to become effective
        logger.info("⏳ Waiting for policies to propagate...")

        def check_policies_ready():
            # We can't directly check policy propagation, but we can verify they exist
            try:
                self.opensearch_client.get_security_policy(name=encryption_policy_name, type='encryption')
                self.opensearch_client.get_security_policy(name=network_policy_name, type='network')
                self.opensearch_client.get_access_policy(name=access_policy_name, type='data')
                return True
            except Exception:
                return False

        # Give policies time to propagate (minimum wait, then verify)
        self._wait_with_exponential_backoff(check_policies_ready, max_attempts=7, base_delay=5, description="policies to propagate")

        # Create collection
        logger.info(f"Creating collection: {collection_name}")
        collection_id = self.create_collection(collection_name)
        if not collection_id:
            return False

        self.collection_arn = f"arn:aws:aoss:{self.region}:{self.account_id}:collection/{collection_id}"

        # Wait for collection to be fully ready with proper polling
        def check_collection_ready():
            try:
                status_response = self.opensearch_client.batch_get_collection(ids=[collection_id])
                status = status_response['collectionDetails'][0]['status']
                return status == 'ACTIVE'
            except Exception:
                return False
        
        if not self._wait_with_exponential_backoff(check_collection_ready, max_attempts=20, description=f"collection {collection_id} to be fully ready"):
            return False
            
        # Wait for data access policy to propagate
        logger.info(f"Waiting for data access policy to propagate for collection {collection_id}...")
        wait_seconds = int(os.environ.get('POLICY_PROPAGATION_WAIT', 15))
        logger.info(f"Waiting {wait_seconds}s for policy propagation")
        time.sleep(wait_seconds)
        
        # Create vector index
        logger.info(f"Creating vector index: {index_name}")
        if not self.create_vector_index(collection_id, index_name):
            return False

        return True


    def run(self):
        """Main execution method"""

        kb_name = f"{self.prefix}-kb-{self.suffix}"
        collection_name = f"{self.prefix}-kb-collection-{self.suffix}"
        index_name = "bedrock-knowledge-base-default-index"
        role_name = f"{self.prefix}-kb-role-{self.suffix}"
        self.role_arn = f"arn:aws:iam::{self.account_id}:role/{role_name}"
        
        # Step 1: Create vector DB and its needed components
        try:
            if not self.create_vector_db(collection_name, index_name):
                logger.error("Failed to create vector DB")
                return False
        except Exception as e:
            logger.error(f"Critical error during vector DB creation: {str(e)}")
            return False
        
        
        # Step 2: Create knowledge bases
        kb_config = {
            'kb_name': kb_name,
            "role_name": role_name,
            'collection_arn': self.collection_arn,
            'index_name': index_name,
            'local_data_path': 'sample-data-knowledge-base',
            's3_prefix': 'manuals-kb'
        }
       
        resources_created = []
        
        try:
            logger.info(f"Processing knowledge base: {kb_config['kb_name']}")
            
            result = self.create_knowledge_base_complete(**kb_config)

            logger.info(f"Knowledge base creation result: {result}")
            
            if not result:
                logger.error(f"Failed to create knowledge base: {kb_config['kb_name']}")
                return False
                    
        except Exception as e:
            logger.error(f"Critical error during knowledge base creation: {str(e)}")            
            return False

        # Print summary
        logger.info("\n" + "="*50)
        logger.info("=== Knowledge Base Creation Summary ===")
        logger.info("="*50)
        logger.info(f"{kb_name}: SUCCESS")
        logger.info(f"   Knowledge Base ID: {result['knowledge_base_id']}")
        logger.info(f"   Collection ARN: {result['collection_arn']}")
        logger.info(f"   Data Source ID: {result['data_source_id']}")
        logger.info(f"   Ingestion Job ID: {result['ingestion_job_id']}")

        return True


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
    parser = argparse.ArgumentParser(description="Deploy knowledge base")
    parser.add_argument("--region", help="AWS region, can also read from AWS_REGION env")
    parser.add_argument("--prefix", help="Project Prefix for resources created, can also read from PROJECT_PREFIX env")
    parser.add_argument("--suffix", help="Project Suffix (4 character) to gurantee resource uniqueness, can also read from PROJECT_SUFFIX env")
    args = parser.parse_args()

    # Get configuration using priority: args > env vars > defaults
    region = get_param(args.region, "AWS_REGION", "us-east-1")
    prefix = get_param(args.prefix, "PROJECT_PREFIX", "sample-asa")
    suffix = get_param(args.suffix, "PROJECT_SUFFIX", str(uuid.uuid4())[:4])

    exit_code = 0

    creator = KnowledgeBaseCreator(region, prefix, suffix)

    exit_code = creator.run()

    return exit_code

if __name__ == "__main__":
    sys.exit(main())
