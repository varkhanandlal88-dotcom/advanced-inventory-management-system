# Advanced Inventory Management System

A professional desktop inventory management application built with **Python, CustomTkinter and SQLite**.

## Features
- Live dashboard with stat cards, recent transactions, low-stock alerts and charts
- Product, category and supplier management with search and filters
- Purchases and sales that update stock automatically
- Sales are blocked when stock is insufficient
- Low-stock alerts based on reorder levels
- Full stock movement history and date-wise reports
- Report export to CSV (opens in Excel)
- Backup and restore
- Admin login with salted PBKDF2 password hashing

## Tech Stack
Python 3.10+, CustomTkinter, SQLite, Matplotlib

## Installation
1. Clone the repository:
   ```
   git clone https://github.com/varkhanandlal88-dotcom/advanced-inventory-management-system.git
   cd advanced-inventory-management-system
   ```
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Run the app:
   ```
   python inventory_system.py
   ```

On first run an admin account is created. Change the password in **Backup & Settings** after logging in.

## Project Structure
Single-file application organised into layers: models, DatabaseManager, business-logic managers (Authentication, Product, Inventory, Purchase, Sales, Report) and the GUI.

## Author
Varkha Motani - www.linkedin.com/in/varkha-motani-98b948414
