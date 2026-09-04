import json
import boto3
import os
import datetime

dynamodb = boto3.resource("dynamodb")
DDB_TABLE_NAME = os.environ.get("DDB_TABLE_NAME")
Version = os.environ.get("TemplateVersion") 

def lambda_handler(event, context):
    """
    Process SQS messages and write to DynamoDB (aggregates table)
    Handles messages for batch and cohort events
    """
    
    print(f"## EVENT: {json.dumps(event)}")
    
    table = dynamodb.Table(DDB_TABLE_NAME)
    processed_count = 0
    
    for record in event['Records']:
        try:
            # Parse SQS message body
            payload = json.loads(record['body'])
            print(f"## PAYLOAD: {json.dumps(payload)}")
            
            # Handle SNS-wrapped messages
            if payload.get('Type') == 'Notification' and 'Message' in payload:
                message = json.loads(payload['Message'])
            else:
                message = payload
            
            # Extract fields (support both old and new formats)
            partition_key = message.get('SampleId') or message.get('sample_id') or message.get('pk')
            event = message.get('Event')
            status = message.get('Status', 'PENDING')
            
            num_completed_samples = message.get('Count_Completed_Entities', 0)
            num_failed_samples = message.get('Count_Failed_Entities', 0)
            num_total_samples = message.get('Count_Total_Entities', 0)
            
            if not partition_key:
                raise ValueError("Missing batch_id")
            
            # Validate this is a batch or cohort event
            if not (partition_key.startswith('BATCH#') or partition_key.startswith('COHORT#')):
                raise ValueError(f"Invalid entity type for aggregates table: {partition_key}")
            
            # Get workflow info from Info object or top-level
            info = message.get('Info', {})
            
            # Build DynamoDB item
            timestamp = datetime.datetime.now(datetime.UTC).isoformat()
            item = {
                'pk': partition_key,  # BATCH#batch_001 or COHORT#cohort_id
                'Event': f"{event}",
                'Status': status,
                'EventStatus': f"{event}#{status}",
                'Count_Total_Entities': num_total_samples, 
                'Count_Completed_Entities': num_completed_samples,
                'Count_Failed_Entities': num_failed_samples,
                'Timestamp': timestamp,
                'Info': info
            }
            
            # Add processed_entities if present in message
            if 'processed_entities' in message:
                processed_entities = message['processed_entities']
                # Convert list back to set for DynamoDB
                if isinstance(processed_entities, list):
                    item['processed_entities'] = set(processed_entities)
                else:
                    item['processed_entities'] = processed_entities
            
            # Add optional fields if present in message
            if message.get('batch_id_initial'):
                item['batch_id_initial'] = message['batch_id_initial']
            if message.get('batch_id_qced'):
                item['batch_id_qced'] = message['batch_id_qced']
            if message.get('cohort_id'):
                item['cohort_id'] = message['cohort_id']
            
            # Create a copy for logging (convert all sets to lists for JSON serialization)
            log_item = {}
            for key, value in item.items():
                if isinstance(value, set):
                    log_item[key] = list(value)
                else:
                    log_item[key] = value
            
            print(f"## DDB Body: {json.dumps(log_item)}")
            
            # Write to DynamoDB
            response = table.put_item(Item=item)
            processed_count += 1
            print(f"✓ Written to DynamoDB: {partition_key} - {event} - {status}")
            
        except Exception as e:
            print(f"✗ Error processing record: {str(e)}")
            print(f"Record body: {record.get('body', 'N/A')}")
            raise
    
    return response
