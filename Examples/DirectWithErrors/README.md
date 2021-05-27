# Appraise Evaluation System

Generating an example campaign with direct assessment and error annotation tasks:

    python manage.py init_campaign Examples/DirectWithErrors/manifest.json \
        --csv-output Examples/DirectWithErrors/output.csv
        
    # From the admin panel, create a campaign with the name 'explainable_quality_estimation'
    # From the admin panel, add batches.json and add the batch to the campaign 'explainable_quality_estimation'

    python manage.py validatecampaigndata explainable_quality_estimation
    python manage.py ProcessCampaignData explainable_quality_estimation DirectWithErrors
    python manage.py UpdateEvalDataModels
    

    # See Examples/Direct/outputs.csv for a SSO login for the annotator account
    # Collect some annotations, then export annotation scores...

    python manage.py ExportSystemScoresToCSV explainable_quality_estimation
