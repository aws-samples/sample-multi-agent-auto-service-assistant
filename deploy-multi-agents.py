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


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('AgentDeployer')


class BedrockAgentManager:
    """Handles creation and management of Amazon Bedrock agents."""

    def __init__(self, prefix, suffix, account_id=None, region_name=None):
        """Initialize with region"""
        self.session = boto3.Session()
        self.region_name = region_name
        self.account_id = account_id
        self.bedrock_client = self.session.client("bedrock-agent", region_name=self.region_name)
        self.iam_client = self.session.client("iam")
        self.prefix = prefix
        self.suffix = suffix

        self.default_model_inference_profile = f"arn:aws:bedrock:{self.region_name}:{self.account_id}:inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0"
        self.default_model_arn = "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
        logger.info(f"Using region: {self.region_name}")


    def create_agent_role(self, role_name, has_action_group=False, has_knowledge_base=False):
        """Create an agent role"""
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AmazonBedrockAgentBedrockFoundationModelPolicyProd",
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
                            "aws:SourceArn": f"arn:aws:bedrock:{self.region_name}:{self.account_id}:agent/*"
                        }
                    }
                }
            ]
        }
        try:
            response = self.iam_client.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
            )

            role_arn = response["Role"]["Arn"]

            policies = [
                {
                    "policy_name": f"AgentBedrockLlmCrossRegionInferenceAccess",
                    "policy": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "AmazonBedrockAgentInferenceProfilesCrossRegionPolicyProd",
                                "Effect": "Allow",
                                "Action": [
                                    "bedrock:InvokeModel",
                                    "bedrock:InvokeModelWithResponseStream",
                                    "bedrock:GetInferenceProfile",
                                    "bedrock:GetFoundationModel"
                                ],
                                "Resource": [
                                    self.default_model_inference_profile,
                                    self.default_model_arn
                                ]
                            }
                        ]
                    }
                }
            ]

            if has_action_group:
                policies.append(
                    {
                        "policy_name": f"AgentBedrockActionGroupExecutionAccess",
                        "policy": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Sid": "AmazonBedrockAgentActionGroupExecutionAccess",
                                    "Effect": "Allow",
                                    "Action": "lambda:InvokeFunction",
                                    "Resource": f"arn:aws:lambda:{self.region_name}:{self.account_id}:function:{self.prefix}-agent-query-history-{self.suffix}"
                                }
                            ]
                        }
                    }
                )
            
            if has_knowledge_base:
                policies.append(
                    {
                        "policy_name": f"AgentBedrockKnowledgeBaseAccess",
                        "policy": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Sid": "AgentBedrockKnowledgeBaseAccess",
                                    "Effect": "Allow",
                                    "Action": [
                                        "bedrock:Retrieve",
                                        "bedrock:RetrieveAndGenerate"                                    
                                    ],
                                    "Resource": f"arn:aws:bedrock:{self.region_name}:{self.account_id}:knowledge-base/*"
                                }
                            ]
                        }
                    }
                )

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

            logger.info(f"Created role: {role_arn}")
            return role_arn
        except Exception as e:
            logger.error(f"Failed to create role: {e}")
            return None


    def add_supervisor_collabrator_role_policy(self, role_name, collabrator_name, collabrator_alias_arn):
        try:
            policy_block = {
                "policy_name": f"MultiAgentPermission{collabrator_name}".replace("-", "").replace("_", ""),
                "policy": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": f"MultiAgentPermission{collabrator_name}".replace("-", "").replace("_", ""),
                            "Effect": "Allow",
                            "Action": [
                                "bedrock:GetAgentAlias",
                                "bedrock:InvokeAgent"
                            ],
                            "Resource": [
                               collabrator_alias_arn
                            ]
                        }
                    ]
                }
            }

            self.iam_client.put_role_policy(
                RoleName=role_name,
                PolicyName=policy_block["policy_name"],
                PolicyDocument=json.dumps(policy_block["policy"])
            )

            # Wait for role to propagate
            logger.info(f"Waiting for collabrator policy to propagate...")
            wait_seconds = int(os.environ.get('POLICY_PROPAGATION_WAIT', 15))
            logger.info(f"Waiting {wait_seconds}s for policy propagation")
            time.sleep(wait_seconds)

            logger.info(f"Attached policy for {collabrator_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to create role: {e}")
            return False
    

    def list_agents(self):
        """List all agents"""
        agents = []
        next_token = None
        try:
            while True:
                params = {"nextToken": next_token} if next_token else {}
                response = self.bedrock_client.list_agents(**params)
                agents.extend(response.get("agentSummaries", []))
                next_token = response.get("nextToken")
                if not next_token:
                    break
            return agents
        except Exception as e:
            logger.error(f"Failed to list agents: {e}")
            return []


    def get_latest_agent_version(self, agent_id):
        """Get latest version of an agent"""
        try:
            versions = []
            next_token = None
            while True:
                params = {"agentId": agent_id}
                if next_token:
                    params["nextToken"] = next_token
                response = self.bedrock_client.list_agent_versions(**params)
                versions.extend(response.get("agentVersionSummaries", []))
                next_token = response.get("nextToken")
                if not next_token:
                    break
            if not versions:
                return None
            latest = max(versions, key=lambda v: v["agentVersion"])
            return latest["agentVersion"]
        except Exception as e:
            logger.error(f"Failed to get agent versions: {e}")
            return None


    def create_agent(self, agent_config):
        """Create a new agent or use existing one"""
        try:
            agent_name = agent_config["agentName"]
            
            # Check if agent already exists
            agents = self.list_agents()
            for agent in agents:
                if agent["agentName"] == agent_name:
                    agent_id = agent["agentId"]
                    agent_version = self.get_latest_agent_version(agent_id)
                    
                    # Check if we need to update the foundation model
                    current_agent = self.bedrock_client.get_agent(agentId=agent_id)
                    current_model = current_agent["agent"]["foundationModel"]
                    desired_model = agent_config["foundationModel"]
                    
                    if current_model != desired_model:
                        logger.info(f"Updating agent '{agent_name}' from {current_model} to {desired_model}")
                        update_params = {
                            "agentId": agent_id,
                            "agentName": agent_name,
                            "foundationModel": desired_model,
                            "agentResourceRoleArn": agent_config["agentResourceRoleArn"],
                            "instruction": agent_config["instruction"],
                            "idleSessionTTLInSeconds": agent_config.get("idleSessionTTLInSeconds", 3600),
                        }
                        
                        # Add optional parameters
                        if "description" in agent_config:
                            update_params["description"] = agent_config["description"]
                        
                        self.bedrock_client.update_agent(**update_params)
                        logger.info(f"Updated agent '{agent_name}' with new foundation model")
                    else:
                        logger.info(f"Using existing agent '{agent_name}' with ID: {agent_id}, version: {agent_version}")
                    
                    return agent_id, agent_version
            
            # If we get here, agent doesn't exist, so create it
            logger.info(f"Creating agent: {agent_name}")
            
            # Extract required parameters
            create_params = {
                "agentName": agent_name,
                "foundationModel": agent_config["foundationModel"],
                "agentResourceRoleArn": agent_config["agentResourceRoleArn"],
                "instruction": agent_config["instruction"],
                "idleSessionTTLInSeconds": agent_config.get("idleSessionTTLInSeconds", 3600),
            }
            
            # Add optional parameters
            if "description" in agent_config:
                create_params["description"] = agent_config["description"]
            if "agentCollaboration" in agent_config:
                create_params["agentCollaboration"] = agent_config["agentCollaboration"]
            # Note: agentCollaborators is not a valid parameter for create_agent
            # We'll handle collaborators after agent creation

                
            response = self.bedrock_client.create_agent(**create_params)
            agent_id = response["agent"]["agentId"]
            logger.info(f"Created agent '{agent_name}' with ID: {agent_id}")

            agent_version = self.get_latest_agent_version(agent_id)
            logger.info(f"Agent version: {agent_version}")

            # Wait for agent to be ready
            self._wait_for_agent_status(agent_id, "CREATING", target_status=None, timeout=300)

            # Add action groups if provided
            if "actionGroups" in agent_config:
                for group in agent_config["actionGroups"]:
                    try:
                        logger.info(f"Adding action group: {group['actionGroupName']}")
                        self.bedrock_client.create_agent_action_group(
                            agentId=agent_id,
                            actionGroupName=group["actionGroupName"],
                            description=group.get("description", "Action group for agent"),
                            actionGroupExecutor=group["actionGroupExecutor"],
                            functionSchema=group.get("functionSchema", {}),
                            agentVersion=group.get("agentVersion", "DRAFT")
                        )
                        logger.info(f"Added action group: {group['actionGroupName']}")
                    except Exception as e:
                        logger.error(f"Failed to add action group {group['actionGroupName']}: {e}")
            
            # Associate knowledge base if provided
            if "knowledgeBase" in agent_config and agent_config["knowledgeBase"].get("knowledge_base_id"):
                try:
                    kb_id = agent_config["knowledgeBase"]["knowledge_base_id"]
                    if kb_id:
                        logger.info(f"Linking knowledge base: {kb_id}")
                        self.bedrock_client.associate_agent_knowledge_base(
                            agentId=agent_id,
                            agentVersion=agent_version,
                            knowledgeBaseId=kb_id,
                            knowledgeBaseState="ENABLED",
                            description=agent_config["knowledgeBase"].get("description", "Knowledge base for agent")
                        )
                        logger.info(f"Knowledge base {kb_id} linked successfully")
                except Exception as e:
                    logger.error(f"Failed to link knowledge base: {e}")

            return agent_id, agent_version
        
        except self.bedrock_client.exceptions.ConflictException:
            logger.warning(f"Agent '{agent_config['agentName']}' already exists.")
            # Try to get the existing agent ID
            agents = self.list_agents()
            for agent in agents:
                if agent["agentName"] == agent_config["agentName"]:
                    agent_id = agent["agentId"]
                    agent_version = self.get_latest_agent_version(agent_id)
                    logger.info(f"Using existing agent '{agent_config['agentName']}' with ID: {agent_id}, version: {agent_version}")
                    return agent_id, agent_version
            return None, None
        except Exception as e:
            logger.error(f"Failed to create agent '{agent_config['agentName']}': {e}")
            return None, None


    def _wait_for_agent_status(self, agent_id, current_status, target_status=None, timeout=300, wait_seconds=5):
        """Wait for agent to reach a status other than current_status or target_status"""
        start_time = time.time()
        while True:
            try:
                response = self.bedrock_client.get_agent(agentId=agent_id)
                status = response['agent']['agentStatus']
                logger.info(f"Agent {agent_id} status: {status}")
                
                if target_status and status == target_status:
                    logger.info(f"Agent {agent_id} reached target status: {target_status}")
                    break
                elif current_status and status != current_status:
                    logger.info(f"Agent {agent_id} is no longer in {current_status} state (now: {status})")
                    break
                    
                if time.time() - start_time > timeout:
                    raise TimeoutError(f"Timeout waiting for agent {agent_id} status change")
                
                time.sleep(wait_seconds)
            except Exception as e:
                logger.warning(f"Propogating error raised while checking agent status: {e}")
                raise


    def prepare_and_publish_agent(self, agent_id, agent_version, alias_name="live"):
        """Prepare and publish an agent with an alias"""
        try:
            logger.info(f"Preparing agent '{agent_id}'")
            self.bedrock_client.prepare_agent(agentId=agent_id)
            
            # Wait for agent to be prepared
            self._wait_for_agent_status(agent_id, "PREPARING", target_status="PREPARED", timeout=300)
            
            # Create new alias
            try:
                response = self.bedrock_client.create_agent_alias(
                    agentId=agent_id,
                    agentAliasName=alias_name
                )
                logger.info(f"Created alias '{alias_name}'")
                return response["agentAlias"]["agentAliasId"]
            except Exception as alias_error:
                logger.error(f"Failed to create alias: {alias_error}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to prepare and publish agent: {e}")
            return None


    def add_collaborator(self, super_id, super_agent_version, collabrator_alias_arn, collaborator_name, collaboration_instruction):
        """Add a collaborator to a supervisor agent"""
        try:
            logger.info(f"Adding collaborator '{collaborator_name}' to agent '{super_id}'")
            
            # Construct the ARN correctly for the agent alias
            logger.info(f"Using ARN: {collabrator_alias_arn}")
            
            # Create the request parameters and log them for debugging
            request_params = {
                "agentDescriptor": {
                    "aliasArn": collabrator_alias_arn
                },
                "agentId": super_id,
                "agentVersion": super_agent_version,
                "collaborationInstruction": collaboration_instruction,
                "collaboratorName": collaborator_name,
                "relayConversationHistory": "DISABLED"
            }
            
            logger.info(f"Associate collaborator request params: {json.dumps(request_params, default=str)}")
            
            response = self.bedrock_client.associate_agent_collaborator(**request_params)
            logger.info(f"Added collaborator '{collaborator_name}' with response: {json.dumps(response, default=str)}")
            return True
        except Exception as e:
            logger.error(f"Failed to add collaborator '{collaborator_name}': {e}")
            return False


def load_agent_configs(account_id, region, prefix, suffix, model_inference_profile=None):
    """Load agent configurations from file or environment and update model choice"""

    def load_prompt(name):
        path = os.path.join(os.path.dirname(__file__), "prompts", f"{name}.txt")
        with open(path, "r", encoding='utf-8') as f:
            return f.read().strip()

    def lookup_kb_id(kb_name):
        client = boto3.client("bedrock-agent")
        response = client.list_knowledge_bases(
            maxResults=100
        )
        for kb in response['knowledgeBaseSummaries']:
            if kb['name'] == kb_name:
                return kb['knowledgeBaseId']
        return None

    default_model_inference_profile = f"arn:aws:bedrock:{region}:{account_id}:inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0"

    if not model_inference_profile:
        model_inference_profile = default_model_inference_profile

    supervisor_agent_config = {
        "agentCollaboration": 'SUPERVISOR',
        "agentName": f"{prefix}-supervisor-{suffix}",
        "agentResourceRoleName": f"{prefix}-AgentSupervisorRole-{suffix}",
        "foundationModel": model_inference_profile,
        "instruction": load_prompt("supervisor"),
        "idleSessionTTLInSeconds": 3600,
    }

    sub_task_agent_configs = [
        {
            "agentName": f"{prefix}-query-history-{suffix}",
            "agentResourceRoleName": f"{prefix}-AgentQueryHisRole-{suffix}",
            "foundationModel": model_inference_profile,
            "instruction": load_prompt("query-history"),
            "idleSessionTTLInSeconds": 3600,
            "actionGroups": [
                {
                    "actionGroupName": "query-history",
                    "actionGroupExecutor": {
                        "lambda": f"arn:aws:lambda:{region}:{account_id}:function:{prefix}-agent-query-history-{suffix}"
                    },
                    "agentVersion": "DRAFT",
                    "functionSchema": {
                        "functions" : [
                            {
                                "name": "query-history",
                                "parameters": {
                                    'last5_vin': {
                                        'description': 'last 5 character of the VIN number',
                                        'required': True,
                                        'type': 'string'
                                    }
                                }

                            }
                        ]
                    }
                }
            ],
            "collaboratorName": "query-history",
            "collaborationInstruction": load_prompt("query-history-collabrator")
        },
        {
            "agentName": f"{prefix}-diagnostic-repair-{suffix}",
            "agentResourceRoleName": f"{prefix}-AgentDiagnoseRole-{suffix}",
            "foundationModel": model_inference_profile,
            "instruction": load_prompt("diagnostic-repair"),
            "idleSessionTTLInSeconds": 3600,
            "knowledgeBase": {
                "knowledge_base_id": lookup_kb_id(f"{prefix}-kb-{suffix}")
            },
            "collaboratorName": "diagnostic-repair",
            "collaborationInstruction": load_prompt("diagnostic-repair-collabrator")   
        }
    ]

    if model_inference_profile:     
        # Update model ARN in all configurations
        supervisor_agent_config["foundationModel"] = model_inference_profile
        for config in sub_task_agent_configs:
            config["foundationModel"] = model_inference_profile
            
    return sub_task_agent_configs, supervisor_agent_config



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
    
    # Get configuration from environment
    account_id = boto3.client('sts').get_caller_identity()['Account']

    # Load agent configurations with model choice
    sub_task_agent_configs, supervisor_agent_config = load_agent_configs(account_id, region, prefix, suffix)
    
    # Initialize agent manager
    manager = BedrockAgentManager(prefix, suffix, account_id=account_id, region_name=region)
    
    if not sub_task_agent_configs or not supervisor_agent_config:
        logger.error("Failed to load agent configurations")
        return False
          
    for config in sub_task_agent_configs:
        try:
            logger.info(f"Processing sub-task agent: {config['agentName']}")

            # Create IAM role for agent
            has_action_group = "actionGroups" in config.keys()
            has_knowledge_base = "knowledgeBase" in config.keys()
            config['agentResourceRoleArn'] = manager.create_agent_role(config['agentResourceRoleName'], has_action_group, has_knowledge_base)
            if not config['agentResourceRoleArn']:
                logger.error(f"Error processing agent {config['agentName']}: Failed to create IAM role")
                return False

            # Create the subtask agent
            config['agentId'], config['agentVersion'] = manager.create_agent(config)
            if not config['agentId'] or not config['agentVersion']:
                logger.error(f"Error processing agent {config['agentName']}: Failed to create agent")
                return False
            
            # Prepare and publish the agent
            config['agentAliasId'] = manager.prepare_and_publish_agent(config['agentId'], config['agentVersion'], "live")
            if config['agentAliasId']:
                logger.info(f"Successfully processed agent: {config['agentName']}")
            else:
                logger.error(f"Failed to publish agent: {config['agentName']} - cannot get agent alias id")
                return False

        except Exception as e:
            logger.error(f"Error processing agent {config['agentName']}: {e}")
            return False

    # Process supervisor agent
    logger.info(f"\nProcessing supervisor agent: {supervisor_agent_config['agentName']}")

    # Create the supervisor agent
    supervisor_agent_config["agentCollaboration"] = "SUPERVISOR"

    # Create IAM role for agent
    supervisor_agent_config['agentResourceRoleArn'] = manager.create_agent_role(supervisor_agent_config['agentResourceRoleName'])
    if not supervisor_agent_config['agentResourceRoleArn']:
        logger.error(f"Error processing agent {supervisor_agent_config['agentName']}: Failed to create IAM role")
        return False
    
    super_agent_id, super_agent_version = manager.create_agent(supervisor_agent_config)
    if not super_agent_id or not super_agent_version:           
        logger.error("Failed to publish supervisor agent")
        return False    
    
    # Add each collaborator using the add_collaborator method

    for config in sub_task_agent_configs:
        try:
            # Use the add_collaborator method from the manager class
            logger.info(f"Adding collaborator '{config['collaboratorName']}' alias '{config['agentAliasId']}' to supervisor agent")

            collabrator_alias_arn = f"arn:aws:bedrock:{region}:{account_id}:agent-alias/{config['agentId']}/{config['agentAliasId']}"

            manager.add_supervisor_collabrator_role_policy(supervisor_agent_config['agentResourceRoleName'], config['agentName'], collabrator_alias_arn)

            success = manager.add_collaborator(
                super_id=super_agent_id,
                super_agent_version=super_agent_version,
                collabrator_alias_arn=collabrator_alias_arn,
                collaborator_name=config['collaboratorName'],
                collaboration_instruction=config['collaborationInstruction']
            )
            
            if not success:
                logger.error(f"Failed to add collaborator '{config['collaboratorName']}'")
                return False
        except Exception as e:
            logger.error(f"Failed to add collaborator '{config['collaboratorName']}': {e}")
            return False

        logger.info(f"Added collaborator '{config['collaboratorName']}'")
    
    # Now prepare the agent again after adding collaborators
    try:
        logger.info(f"Preparing supervisor agent again after adding {len(sub_task_agent_configs)} collaborators")

        manager.bedrock_client.prepare_agent(agentId=super_agent_id)

        manager._wait_for_agent_status(super_agent_id, "PREPARING", target_status="PREPARED", timeout=300)

        super_alias_id = manager.prepare_and_publish_agent(super_agent_id, super_agent_version, "live")

        logger.info(f"Created alias 'live' for supervisor agent with ID: {super_alias_id}")

    except Exception as e:
        logger.error(f"Failed to create supervisor agent alias: {e}")
        return False

    logger.info("\n=== Deployment Summary ===")
    logger.info(f"Supervisor Agent Name: {supervisor_agent_config['agentName']}")
    logger.info(f"Supervisor Agent ID: {super_agent_id}")
    logger.info(f"Supervisor Agent Alias ID: {super_alias_id}")    
    
    logger.info("\nAgent deployment process completed")
    
    return True

if __name__ == "__main__":
    sys.exit(main())
