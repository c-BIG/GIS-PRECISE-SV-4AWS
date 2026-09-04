The json files on the top level contain file location which will be used to population Healthomics input json, 
which includes the static files like genome reference and reference panel.

Edit this file to reflect the S3 location of your own setup. 
ie where are your reference fasta is found here in json format
{
"reference_fasta": "s3://<your_bucket>/Homo_sapiens_assembly38.fasta",
}

# In templates, these are the json files which will be used to decide which parameters will be activated or included in the Healthomics input json file
