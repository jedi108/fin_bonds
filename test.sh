#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

echo "Running tests..."

echo "1. Clearing data..."
python main.py clear_data

echo "2. Seeding data..."
python main.py seed_data

echo "3. Updating ratings..."
python main.py update_ratings

echo "4. Checking for changes..."
python main.py check_changes

echo "5. Generating plots..."
python main.py generate_plots

echo "6. Exporting bonds..."
python main.py export_bonds _output_/bonds_export.csv

echo "All tests passed successfully!" 


curl "https://iss.moex.com/iss/securities/RU000A1038V6.json?iss.meta=off"

curl "https://iss.moex.com/iss/securities.json?q=RU000A105TS5&iss.meta=off&securities.columns=secid,primary_boardid,type,group"

сurl "https://iss.moex.com/iss/engines/stock/markets/bonds/securities/RU000A105TS5.json?iss.meta=off"

