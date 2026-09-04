import json
import boto3
import os
import datetime

dynamodb = boto3.resource("dynamodb")
DDB_TABLE_NAME = os.environ.get("DDB_TABLE_NAME")
Version = os.environ.get("TemplateVersion") 

def lambda_handler(event, context):
    """
    Process SQS messages and write to DynamoDB (gatk-sv-dev table)
    Handles messages from GatksvSubmitter (Glacier restore) and other sources
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
            
            # print(f"## MESSAGE: {json.dumps(message)}")
            
            # Extract fields (support both old and new formats) from pk -> need to remove the prefix -> single-sample is SAMPLE#<sample_id>
            pk = message.get('pk') or message.get('SampleId')
            sample_id = pk.removeprefix('SAMPLE#')
            event = message.get('Event')
            status = message.get('Status', 'Pending')
            # Respect the timestamp of originating msg -> all msg must come with Timestamp 
            current_timestamp = datetime.datetime.now(datetime.UTC).isoformat()
            timestamp = message.get('Timestamp') or current_timestamp
            output_url = message.get('OutputUrl', '') 
            
            if not sample_id:
                raise ValueError("Missing sample_id or SampleId")
            
            # Get workflow info from Info object or top-level
            info_map = message.get('Info', {})
            # info_map = message.get('Info', {}).get('M', {})
            
            # Build DynamoDB item
            
            item = {
                'pk': f"SAMPLE#{sample_id}",
                'sk': f"{event}#{timestamp}",
                'Event': event,
                'Status': status,
                'EventStatus': f"{event}#{status}",
                'ProcessVersion': Version,
                'Timestamp': timestamp,
                'Info': info_map
            }
            
            output_url = message.get('OutputUrl')
            if output_url:
                item['OutputUrl'] = output_url
            
            # Promote optional top-level fields for monitoring/scanning
            if message.get('batch_id_initial'):
                item['batch_id_initial'] = message['batch_id_initial']
            if message.get('gender'):
                item['gender'] = message['gender']
            if message.get('batch_id_qced'):
                item['batch_id_qced'] = message['batch_id_qced']
            
            print(f"## DDB Body: {json.dumps(item)}")
            
            # Write to DynamoDB
            response = table.put_item(Item=item)
            processed_count += 1
            print(f"✓ Written to DynamoDB: {sample_id} - {event} - {status}")
            
        except Exception as e:
            print(f"✗ Error processing record: {str(e)}")
            print(f"Record body: {record.get('body', 'N/A')}")
            raise
    
    return response

