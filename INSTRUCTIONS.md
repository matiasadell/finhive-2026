This are the instructions for the new PR.

Objectives:
1. Implementation of CI/CD for promoting jobs
2. Implementation of Fred and Yahoo ingestion pipelines

# Instructions


1. Read docs/architecture/ARCHITECTURE_V2.md
2. Review and correct notebooks/setup/boostrap_secrets.ipynb
3. implement the CI/CD github actions according to the architecture documentation
4. implement the data ingestion jobs for Fred API macroeconomic data and Yahoo Finance data
5. Create commit and push to remote
6. Create a pull request to main

## How to make the ingestion jobs

### Yahoo finance

This job is composed of an orchestrator notebook and a worker notebook.
The orchestrator notebook iterates over all the configuration series that should be stored in `config/data_ingestion/yahoo.json`. the json structure is as follows:
```json
[
    {"series":"^GSPC","start_date":null},
    {"series":"GOOG","start_date":"1997-03-23"}
]
```
This is ilustrative. The starting dates should start with `null` which in json.loads() translates to `None`, which is the value yfinance accepts for bringing the enitre history.

For each iteration it should call the worker notebook with `dbutils.notebook.run()` and capture it's output. The arguments for the notebook should be the series, the starting date and the name of the job retrieved using `dbutils.widgets.get()`. The orchestrator should capture its output and log it to `finhive.logs.ingestionLog`. The structure of the `ingestionLog` table is:

|Column| Data Type|
|------|----------|
|series|String|
|source| String| # Fred, Yahoo, etc.
|status| bool| # True for pass False for failure
|updateAt|datetime|
|item_count| int| # rows,documents, etc. so its flexible to multiple data sources
|error|string|
|job_name| string|

#### The worker notebook
The worker notebook gathers all the parameters passed to it with `dbutils.widgets.get()` and then does a try except. the try does the following:
1. download the series. it downloads as a pandas dataframe
2. convert it to a spark dataframe
3. add metadata columns for the timestamp generated at and the pipeline name
4. append that dataframe since it will be ingesting only the new data to `finhive.yahoo.<series>`. the catalog and schema have to be created in the worker notebook with `spark.sql("CREATE CATALOG IF NOT EXISTS finhive")` and `spark.sql("CREATE SCHEMA IF NOT EXISTS finhive.yahoo")` respectively.
5. return with `dbutils.notebook.exit()` the status True, the series, the source, and the timestamp, and the count, the job name and error as `None` (basically everything the log needs).
6. if it fails it should exit the same but with the exception as the error and the status as False.

### Logging
Once all the series are done the orchestrator will capture all the results and append them to `finhive.logs.ingestionLog`. after that it should check if one of the statuses is False and if that is the case it should raise an error so that the job itself fails at the end and it is red in the job run monitor to show that at least one worker failed. the catalog and schema for the log table have to be created the same way the series catalog and schema are created.

The date should be logged to the config json file too so that it is used for the next run.

#### Trigger
The job should trigger every weekday when the market closes

#### Parameters
just the job name for logging


### FRED
The FRED job is basically the same as the yahoo one except that it needs the fred api key. that key will be read as a secret inside the woker notebook.

