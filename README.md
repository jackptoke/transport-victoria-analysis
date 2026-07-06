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
