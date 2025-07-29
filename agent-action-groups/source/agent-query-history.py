#!/usr/bin/env python3
import csv
import logging
import time

# Configure the logger for AWS Lambda
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('AgentQueryHistory')


def lambda_handler(event, context):
    # Start timing the function execution
    start_time = time.time()
    
    agent = event['agent']
    actionGroup = event['actionGroup']
    function = event['function']
    parameters = event.get('parameters', [])
    input_params = {param["name"]: param["value"] for param in parameters}
    logger.info(f"Event: {event}")
    logger.info(f"Input parameters: {input_params}")

    try:
        if input_params.get("last5_vin"):
            vehicle_id = input_params["last5_vin"].lower()
            logger.info(f"Querying service history for vehicle ID: {vehicle_id}")
            
            # Read CSV file
            csv_file_path = "service_records_star.csv"
            simplified_response = []
            
            with open(csv_file_path, 'r', encoding='utf-8') as file:
                csv_reader = csv.DictReader(file)
                for row in csv_reader:
                    if row.get("VehicleABIEType_VehicleID", "").lower() == vehicle_id:
                        simplified_item = {
                            "date": row.get("RepairOrderCompletedDate", "Unknown date"),
                            "service": row.get("Diagnostics", "Unknown service"),
                            "notes": row.get("OrderNotes", "No notes available"),
                            "mileage": row.get("MileageInDistance", "Unknown mileage")
                        }
                        simplified_response.append(simplified_item)
            
            logger.info(f"Found {len(simplified_response)} service history records")
            response = simplified_response if simplified_response else "No service history found for this vehicle."
        else:
            response = "Please provide a valid last5_vin."

    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        response = f"Error retrieving service history: {str(e)}"

    # Check execution time
    execution_time = time.time() - start_time
    logger.info(f"Function execution time: {execution_time:.2f} seconds")

    responseBody = {
        "TEXT": {
            "body": f"Here's the service history for vehicle {input_params.get('last5_vin', '')}: {response}"
        }
    }

    action_response = {
        'actionGroup': actionGroup,
        'function': function,
        'functionResponse': {
            'responseBody': responseBody
        }
    }

    result = {'response': action_response, 'messageVersion': event['messageVersion']}
    return result
