# Transport Victoria Updates

## Commands

### Create the app

```terminal
az deployment group create \
  --resource-group transport-victoria-rg \
  --template-file infra/main.bicep \
  --parameters @infra/main.parameters.json
```

### Push the code

``` terminal
cd ingestion && func azure functionapp publish transport-vic-data-download-app

```

### Verify if the function is running

```terminal
func azure functionapp logstream transport-vic-data-download-app
```

### Start/Stop the function

```terminal
az functionapp stop  --name transport-vic-data-download-app --resource-group transport-victoria-rg
# later, to resume every-5-min runs:
az functionapp start --name transport-vic-data-download-app --resource-group transport-victoria-rg
```

#### Delete compute (keeps your storage + data)

```terminal
az functionapp delete       --name transport-vic-data-download-app --resource-group transport-victoria-rg
az appservice plan delete   --name transport-vic-data-download-app-plan --resource-group transport-victoria-rg --yes
```

### Bringing the app back to life

```terminal
az deployment group create --resource-group transport-victoria-rg --template-file main.bicep --parameters @main.parameters.json
func azure functionapp publish transport-vic-data-download-app
```

### Fixing role issues

```terminal
# your user's object id
ME=$(az ad signed-in-user show --query id -o tuple 2>/dev/null || az ad signed-in-user show --query id -o tsv)

az role assignment create \
  --assignee "$ME" \
  --role "Storage Blob Data Reader" \
  --scope "$(az storage account show --name transportvictoriastorage --resource-group transport-victoria-rg --query id -o tsv)"
```

### Billing Alert

```terminal
az consumption budget create \
  --budget-name transport-vic-monthly \
  --amount 5 \
  --time-grain Monthly \
  --category Cost \
  --resource-group transport-victoria-rg \
  --start-date 2026-07-01 \
  --end-date 2027-07-01
```

### Data Reset and Redeployment

databricks bundle deploy -t dev        # 1. push the edited notebook  ← the missing step
dbutils.fs.rm("/Volumes/transport_vic_dev/02_silver/_checkpoints/vehicle_positions", True)   # 2.
DROP TABLE transport_vic_dev.`02_silver`.vehicle_positions;                                   # 3.
databricks bundle run silver_vehicle_positions -t dev    # 4. now runs the NEW notebook
