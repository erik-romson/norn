# Plan — TST-1 (synthetic)

## Summary

Split a slow query into two.

## For the human (run report)

### 1. Flyway patch must be applied to the database repo

Add the patch `V12__split_index.sql` to the database repository before deploy.

### 2. Deploy notes

Restart the worker after the deploy.

### 3. Post-deploy verification query

Run the supplier lookup and check that it returns in under a second.

```sql
-- example
## not a heading
select 1;
```

### 4. Background

The old query joined on an OR condition.

### 5. Follow-ups noticed, not changed in this run

- The legacy report still uses the old query.

## Pre-plan acceptance criteria coverage

All criteria covered.
