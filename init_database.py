import sys
sys.path.insert(0, '.')

from src.database.init_db import db

db.initialise()

print()
print('Database ready. Table status:')
for table, count in db.get_schema_stats().items():
    print(f'  {table}: {count} rows')

db.close()
print()
print('Done. Check your storage/ folder for neural_ledger.db')