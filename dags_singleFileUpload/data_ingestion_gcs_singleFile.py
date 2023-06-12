import os
import logging

from airflow import DAG
from airflow.utils.dates import days_ago
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
import pyarrow.csv as pv
import pyarrow.parquet as pq
from google.cloud import storage
from airflow.providers.google.cloud.operators.bigquery import BigQueryCreateExternalTableOperator

#set up logging
logger = logging.getLogger(__name__)

#variables
#load_dotenv()

dataset_file = "yellow_tripdata_2021-01.csv.gz"
dataset_url = f"https://github.com/DataTalksClub/nyc-tlc-data/releases/download/yellow/{dataset_file}"
path_to_local_home = os.environ.get("AIRFLOW_HOME", "/opt/airflow/")
parquet_file = dataset_file.replace('.csv.gz', '.parquet')

logger.info(f'Dataset file is {dataset_file}, url is {dataset_url}, local_home is {path_to_local_home}, parquet file is {parquet_file}')

#GCP environment variables
BUCKET=os.environ.get("GCP_GCS_BUCKET")
PROJECT_ID=os.environ.get("GCP_PROJECT_ID")
PROJECT_NAME=os.environ.get("GOOGLE_CLOUD_PROJECT")
BIGQUERY_DATASET=os.environ.get("BIGQUERY_DATASET", 'trips_data_all')

logger.info(f'{BUCKET} is the bucket, {PROJECT_NAME} is the project name, {PROJECT_ID} is the project id, {BIGQUERY_DATASET} is the bigquery dataset.')

#set default args for the dag
default_args = {
    "owner": "admin",
    "start_date": days_ago(1),
    "depends_on_past": False,
    "retries": 1,
}


def format_to_parquet(src_file):
    if not (src_file.endswith('.csv') or src_file.endswith('.csv.gz')):
        logger.error("Can only accept source files in CSV format, for the moment")
        return
    table = pv.read_csv(src_file)
    if(src_file.endswith('.csv')):
        pq.write_table(table, src_file.replace('.csv', '.parquet'))
    else:
        pq.write_table(table, src_file.replace('.csv.gz', '.parquet'))


def upload_to_gcs(bucket, object_name, local_file):
    """
    Ref: https://cloud.google.com/storage/docs/uploading-objects#storage-upload-object-python
    :param bucket: GCS bucket name
    :param object_name: target path & file-name
    :param local_file: source path & file-name
    :return:
    """
    # WORKAROUND to prevent timeout for files > 6 MB on 800 kbps upload speed.
    # (Ref: https://github.com/googleapis/python-storage/issues/74)
    storage.blob._MAX_MULTIPART_SIZE = 5 * 1024 * 1024  # 5 MB
    storage.blob._DEFAULT_CHUNKSIZE = 5 * 1024 * 1024  # 5 MB
    # End of Workaround

    client = storage.Client()
    #bucket = client.bucket(bucket)
    bucket = client.get_bucket(bucket)

    blob = bucket.blob(object_name)
    logger.info(f'Blob is {blob}')
    blob.upload_from_filename(local_file)



with DAG(dag_id="data_ingestion_gcs_dag",
         schedule_interval="@daily",
         default_args=default_args,
         catchup=False,
         max_active_runs=1,
         tags=['dtc-de']) as dag:
    
    download_dataset_task = BashOperator(
        task_id="download_dataset_task",
        bash_command=f"curl -sSL {dataset_url} > {path_to_local_home}/{dataset_file}"
    )

    format_to_parquet_task = PythonOperator(
        task_id="format_to_parquet_task",
        python_callable=format_to_parquet,
        op_kwargs={
            "src_file": f"{path_to_local_home}/{dataset_file}"
        }
    )

    local_to_gcs_task = PythonOperator(
        task_id="local_to_gcs_task",
        python_callable=upload_to_gcs,
        op_kwargs={
            "bucket": BUCKET,
            "object_name": f"raw/{parquet_file}",
            "local_file": f"{path_to_local_home}/{parquet_file}"
        }
    )

    bigquery_external_table_task = BigQueryCreateExternalTableOperator(
        task_id="bigquery_external_table_task",
        table_resource={
            "tableReference": {
                "projectId": PROJECT_ID,
                "datasetId": BIGQUERY_DATASET,
                "tableId": "external_table"
            },
            "externalDataConfiguration": {
                "sourceFormat": "PARQUET",
            "sourceUris": [f"gs://{BUCKET}/raw/{parquet_file}"]
            }
        }
    )


download_dataset_task >> format_to_parquet_task >> local_to_gcs_task >> bigquery_external_table_task