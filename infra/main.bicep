// main.bicep — Flex Consumption Function App for the Transport Victoria downloader
param location string = resourceGroup().location
param functionAppName string
param storageAccountName string        // your EXISTING account
@secure()
param gtfsContainerSasUrl string       // passed in, never hardcoded
@secure()
param transportVicApiKey string

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName             // reference, don't recreate
}

// Build the account connection string from the existing account's keys.
// listKeys() reads the live key at deploy time — no secret is hardcoded here.
var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storage.listKeys().keys[0].value}'

// Flex needs a dedicated blob container to hold the deployed app package.
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' existing = {
  parent: storage
  name: 'default'
}
resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'deploymentpackage'
}

// Flex Consumption hosting plan (FC1). Serverless, pay-per-execution.
resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${functionAppName}-plan'
  location: location
  kind: 'functionapp'       // Flex plan uses 'functionapp', not 'linux'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true          // true = Linux (Flex is Linux-only)
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    // functionAppConfig is the defining block of a Flex app: it declares the
    // runtime and where the deployment package lives (the container above).
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}deploymentpackage'
          authentication: {
            type: 'StorageAccountConnectionString'
            storageAccountConnectionStringName: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
    siteConfig: {
      appSettings: [
        { name: 'AzureWebJobsStorage', value: storageConnectionString }
        // Flex reads/writes its deployment package via this connection string:
        { name: 'DEPLOYMENT_STORAGE_CONNECTION_STRING', value: storageConnectionString }
        { name: 'GTFS_CONTAINER_SAS_URL', value: gtfsContainerSasUrl }
        { name: 'TRANSPORT_VIC_API_KEY', value: transportVicApiKey }
      ]
    }
  }
  dependsOn: [
    deploymentContainer
  ]
}
