import json
import boto3
import os
import datetime
from boto3.dynamodb.conditions import Key

# Environment param
SNS_AGGREGATES_ARN      = os.environ['SNS_AGGREGATES_ARN']
TABLE_NAME              = os.environ.get('DDB_TABLE_NAME', 'gatk-sv-dev')
AGGREGATES_TABLE_NAME   = os.environ.get('AGGREGATES_TABLE_NAME', 'gatk-sv-dev-aggregates')

dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')

def lambda_handler(event, context):
    """
    Batch readiness checker - triggered by SNS from status monitor
    Handles batch verification and EvidenceQC triggering
    """
    
    print(f"Event: {json.dumps(event)}")
    
    # Handle SNS trigger from status monitor
    for record in event['Records']:
        if 'Sns' in record:
            message = json.loads(record['Sns']['Message'])
            batch_id = message['batch_id']
            action = message.get('action', 'verify_and_trigger_evidenceqc')
            
            print(f"Processing batch {batch_id} with action: {action}")
            
            if action == 'verify_and_trigger_evidenceqc':
                handle_batch_verification_and_trigger(batch_id, message)
    
    return {'statusCode': 200}

def batch_record_exists(batch_id, workflow_stage):
    """Check if BATCH# record already exists by querying aggregates table"""
    
    aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
    
    try:
        # FIX: Use query for composite key table
        response = aggregates_table.query(
            KeyConditionExpression='pk = :pk',
            ExpressionAttributeValues={':pk': f'BATCH#{batch_id}'},
            Limit=1
        )
        
        items = response.get('Items', [])
        if items:
            existing_event = items[0].get('Event')
            return existing_event == workflow_stage
        
        return False
        
    except Exception as e:
        print(f"Error checking batch record existence: {e}")
        return False


def create_evidenceqc_batch_record(batch_id):
    """Create EvidenceQC batch record using processed_entities from batch definition"""
    
    # Get batch definition from aggregates table
    batch_def = get_batch_definition(batch_id)
    if not batch_def:
        print(f"No batch definition found for {batch_id}")
        return
    
    # Use processed_entities as the verified sample list
    processed_entities_set = batch_def.get('processed_entities', set())
    if isinstance(processed_entities_set, list):
        processed_entities_set = set(processed_entities_set)
    
    total_samples = len(processed_entities_set)
    
    print(f"Using processed_entities: {total_samples} samples")
    
    # Size check - estimate message size (convert to list only for size estimation)
    estimated_size = len(json.dumps(list(processed_entities_set))) + 500  # Base message overhead
    
    if estimated_size < 200_000:  # 200 KB safety margin
        # Direct sample list for small batches
        sample_list = list(processed_entities_set)  # Convert only when needed
        info = {
            'triggered_by': 'batch_readiness_checker'
        }
        print(f"Using direct sample_list for batch {batch_id}: {len(sample_list)} samples (~{estimated_size} bytes)")
        print(f"Sample list content: {sample_list}")
    else:
        # Reference to batch definition for large batches
        info = {
            'batch_id': batch_id,
            'batch_definition_key': f'BATCH_DEF#{batch_id}',
            'total_samples': total_samples,
            'triggered_by': 'batch_readiness_checker'
        }
        print(f"Using batch_definition_key for batch {batch_id}: {len(sample_list)} samples (~{estimated_size} bytes)")
    
    timestamp = datetime.datetime.now(datetime.UTC)
    message = {
        'pk': f'BATCH#{batch_id}',
        'Event': 'EvidenceQC',
        'Status': 'PENDING',
        'Count_Total_Entities': total_samples,
        'Timestamp': timestamp.isoformat(),
        'processed_entities': list(processed_entities_set),  # Convert set to list for JSON serialization
        'Info': info
    }
    
    print(f"EvidenceQC msg: {json.dumps(message)}")
    
    # Send SNS message to parent stack
    try:
        response = sns.publish(
            TopicArn = SNS_AGGREGATES_ARN,
            Message = json.dumps(message),
            MessageGroupId = batch_id,
            MessageDeduplicationId=f"{batch_id}",
            Subject=f'Batch Ready: EvidenceQC-{batch_id}'
        )
        
        print(f"✓ Created EvidenceQC batch record for {batch_id} (MessageId: {response['MessageId']})")
    except Exception as e:
        print(f"✗ Error sending batch message: {str(e)}")
        raise


def get_batch_definition(batch_id):
    """Get batch definition from aggregates table"""
    try:
        aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
        
        # Query to find the batch record (composite key table)
        response = aggregates_table.query(
            KeyConditionExpression='pk = :pk',
            ExpressionAttributeValues={':pk': f'BATCH#{batch_id}'},
            Limit=1
        )
        
        items = response.get('Items', [])
        return items[0] if items else None
        
    except Exception as e:
        print(f"Error getting batch definition for {batch_id}: {e}")
        return None

def handle_batch_verification_and_trigger(batch_id, message):
    """Handle batch verification and EvidenceQC triggering from status monitor"""
    
    completed_samples = message.get('Count_Completed_Entities', 0)
    failed_samples = message.get('Count_Failed_Entities', 0)
    total_samples = message.get('Count_Total_Entities', 0)
    
    # Get processed_entities directly from the message (sent by status monitor)
    processed_entities_set = message.get('processed_entities', set())
    if isinstance(processed_entities_set, list):
        processed_entities_set = set(processed_entities_set)
    
    print(f"Batch {batch_id}: {completed_samples} completed, {failed_samples} failed, {total_samples} total")
    print(f"Processed samples received: {len(processed_entities_set)} samples")
    
    # Handle failed samples - check retry logic
    if failed_samples > 0:
        retry_decision = handle_failed_samples(batch_id, failed_samples)
        if retry_decision == 'RETRY_PENDING':
            print(f"Batch {batch_id}: Retries scheduled, waiting...")
            return
        elif retry_decision == 'RETRY_EXHAUSTED':
            print(f"Batch {batch_id}: Some samples failed all retries")
    
    # All samples completed successfully
    if completed_samples == total_samples:
        # Verify processed_entities contains all expected samples from Info.entity_member
        batch_def = get_batch_definition(batch_id)
        if batch_def:
            entity_member = batch_def.get('Info', {}).get('entity_member', [])
            if entity_member:
                expected = set(entity_member)
                missing = expected - processed_entities_set
                if missing:
                    print(f"⚠️ Batch {batch_id}: Count matches but {len(missing)} samples missing from processed_entities: {missing}")
                    return
                print(f"✓ Batch {batch_id}: All {len(expected)} entity_member verified in processed_entities")
        
        print(f"Batch {batch_id}: All samples completed, updating status and triggering EvidenceQC")
        
        # Update batch record status to COMPLETED
        update_batch_status_to_completed(batch_id, processed_entities_set)
        
        # Use processed_entities as the verified sample list
        create_evidenceqc_batch_record_verified(batch_id, processed_entities_set)
    else:
        print(f"Batch {batch_id}: Has failures ({failed_samples}), handling partial completion")
        handle_partial_batch_completion(batch_id, completed_samples, failed_samples)


def handle_failed_samples(batch_id, failed_count):
    """Handle failed samples - check if retries are needed"""
    
    # For now, simple logic - could be enhanced with retry tracking
    print(f"Batch {batch_id}: {failed_count} failed samples, checking retry eligibility")
    
    # For now, assume no retries pending (can be enhanced)
    return 'RETRY_EXHAUSTED'



def handle_partial_batch_completion(batch_id, completed_count, failed_count):
    """Handle batch with some failed samples"""
    
    print(f"Batch {batch_id}: Partial completion - {completed_count} completed, {failed_count} failed")
    
    # Policy decision: proceed with completed samples or fail entire batch
    # For now, log and wait for manual intervention
    print(f"Batch {batch_id}: Requires manual review for partial completion")




def update_batch_status_to_completed(batch_id, processed_entities_set):
    """Update batch record status to COMPLETED after verification"""
    try:
        aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
        
        response = aggregates_table.update_item(
            Key={
                'pk': f'BATCH#{batch_id}',
                'Event': 'GatherSampleEvidence'
            },
            UpdateExpression='SET #status = :status, last_updated = :timestamp, EventStatus = :EventStatus, processed_entities = :processed_entities',
            ExpressionAttributeNames={
                '#status': 'Status'
            },
            ExpressionAttributeValues={
                ':status': 'COMPLETED',
                ':EventStatus' : 'GatherSampleEvidence#COMPLETED',
                ':timestamp': datetime.datetime.now(datetime.UTC).isoformat(),
                ':processed_entities': processed_entities_set
            }
        )
        
        print(f"✓ Updated batch {batch_id} GatherSampleEvidence status to COMPLETED with {len(processed_entities_set)} processed samples")
        
    except Exception as e:
        print(f"Error updating batch status for {batch_id}: {e}")


def create_evidenceqc_batch_record_verified(batch_id, completed_samples_set):
    """Create EvidenceQC batch record after verification"""
    
    # Size check for sample list
    sample_list = list(completed_samples_set)  # Convert to list for size estimation
    estimated_size = len(json.dumps(sample_list)) + 500
    
    if estimated_size < 200_000:  # 200 KB safety margin
        info = {
            'triggered_by': 'batch_readiness_checker_verified'
        }
    else:
        info = {
            'triggered_by': 'batch_readiness_checker_verified'
        }
    
    message = {
        'pk': f'BATCH#{batch_id}',
        'Event': 'EvidenceQC',
        'Status': 'PENDING',
        'Timestamp': datetime.datetime.now(datetime.UTC).isoformat(),
        'Count_Total_Entities': len(completed_samples_set),
        'processed_entities': list(completed_samples_set),  # Convert set to list for JSON serialization
        'Info': info
    }
    
    print(f"Message: {json.dumps(message)}")
    
    # Send SNS message to aggregates
    try:
        completed_sample_count = len(completed_samples_set)
        response = sns.publish(
            TopicArn=SNS_AGGREGATES_ARN,
            Message=json.dumps(message),
            MessageGroupId=batch_id,
            MessageDeduplicationId=f"{batch_id}-Completed-{completed_sample_count}",
            Subject=f'Batch Ready: EvidenceQC-{batch_id}'
        )
        
        print(f"✓ Created EvidenceQC batch record for {batch_id} (MessageId: {response['MessageId']})")
        
    except Exception as e:
        print(f"✗ Error creating EvidenceQC batch record: {str(e)}")
        raise
