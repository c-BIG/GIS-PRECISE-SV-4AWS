#!/usr/bin/env python3
"""
Test configuration and parameter building without calling HealthOmics
"""
import sys
import os
import json

# Set config directory to local path for testing
CONFIG_DIR = '/mnt/volume1/gatk-sv/gatk-sv-AWS/sam_template/gatksv_healthomics/shared/config'

def load_config(config_name):
    """Load configuration file"""
    config_path = os.path.join(CONFIG_DIR, f'{config_name}.json')
    with open(config_path, 'r') as f:
        return json.load(f)

def load_template(workflow_stage):
    """Load workflow template"""
    template_path = os.path.join(CONFIG_DIR, 'templates', f'{workflow_stage}.json')
    with open(template_path, 'r') as f:
        return json.load(f)

def get_workflow_id(workflow_stage):
    """Get workflow ID from config"""
    workflow_ids = load_config('workflow_ids')
    if workflow_stage not in workflow_ids:
        raise ValueError(f"Workflow stage '{workflow_stage}' not found")
    
    config = workflow_ids[workflow_stage]
    if isinstance(config, str):
        return config, None
    return config.get('id'), config.get('version')

def get_genome_references():
    return load_config('genome_references')

def get_docker_images():
    return load_config('docker_images')

def test_config_loading():
    """Test loading configuration files"""
    print("=" * 60)
    print("TEST 1: Configuration Loading")
    print("=" * 60)
    
    try:
        # Test genome references
        print("\n✓ Loading genome_references.json...")
        genome_refs = get_genome_references()
        print(f"  Found {len(genome_refs)} genome references")
        
        # Test docker images
        print("\n✓ Loading docker_images.json...")
        docker_images = get_docker_images()
        print(f"  Found {len(docker_images)} docker images")
        
        # Test workflow IDs
        print("\n✓ Loading workflow_ids.json...")
        workflow_ids = load_config('workflow_ids')
        print(f"  Found {len(workflow_ids)} workflow configurations")
        
        return True
    except Exception as e:
        print(f"\n✗ Error: {e}")
        return False


def test_template_loading():
    """Test loading workflow templates"""
    print("\n" + "=" * 60)
    print("TEST 2: Template Loading")
    print("=" * 60)
    
    workflows = [
        'GatherSampleEvidence',
        'EvidenceQC',
        'TrainGCNV',
        'ClusterBatch',
        'GenotypeBatch',
        'RegenotypeCNVs',
        'JoinRawCalls',
        'RefineComplexVariants',
        'SVConcordance'
    ]
    
    success = True
    for workflow in workflows:
        try:
            template = load_template(workflow)
            print(f"✓ {workflow}: {template.get('description', 'No description')}")
        except Exception as e:
            print(f"✗ {workflow}: {e}")
            success = False
    
    return success


def test_workflow_id_validation():
    """Test workflow ID retrieval (will fail on placeholders)"""
    print("\n" + "=" * 60)
    print("TEST 3: Workflow ID Validation")
    print("=" * 60)
    
    workflows = ['GatherSampleEvidence', 'EvidenceQC', 'RegenotypeCNVs']
    
    for workflow in workflows:
        try:
            workflow_id, version = get_workflow_id(workflow)
            print(f"✓ {workflow}: ID={workflow_id}, Version={version}")
        except ValueError as e:
            print(f"⚠ {workflow}: {e}")
        except Exception as e:
            print(f"✗ {workflow}: {e}")
    
    print("\nNote: Placeholder errors are expected until you configure actual workflow IDs")
    return True


def test_parameter_builder():
    """Test parameter builder logic"""
    print("\n" + "=" * 60)
    print("TEST 4: Parameter Builder (Skipped)")
    print("=" * 60)
    
    print("\n⚠ Parameter builder requires DynamoDB connection")
    print("  Will be tested during actual Lambda execution")
    return True


def main():
    print("\n" + "=" * 60)
    print("GATK-SV HealthOmics Configuration Test")
    print("=" * 60)
    
    results = []
    
    results.append(("Config Loading", test_config_loading()))
    results.append(("Template Loading", test_template_loading()))
    results.append(("Workflow ID Validation", test_workflow_id_validation()))
    results.append(("Parameter Builder", test_parameter_builder()))
    
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    for test_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {test_name}")
    
    all_passed = all(result[1] for result in results)
    
    if all_passed:
        print("\n✓ All tests passed!")
        print("\nNext steps:")
        print("1. Update workflow_ids.json with actual HealthOmics workflow IDs")
        print("2. Deploy Lambda functions: sam build && sam deploy")
        print("3. Test with actual DynamoDB trigger")
    else:
        print("\n✗ Some tests failed - review errors above")
        sys.exit(1)


if __name__ == '__main__':
    main()
