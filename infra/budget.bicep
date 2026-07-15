resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'transport-vic-monthly'
  properties: {
    category: 'Cost'
    amount: 5
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: '2026-07-01'   // first of the month
    }
    notifications: {
      actualOver80: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        contactEmails: [ 'jack.toke@grampians.ai' ]
      }
    }
  }
}
