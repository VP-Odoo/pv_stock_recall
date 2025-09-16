# -*- coding: utf-8 -*-
{
    "name": "PV Stock Recall Orchestrator ",
    "version": "18.0.1.0.3",  # bump to ensure upgrade applies
    "category": "Inventory/Inventory",
    "summary": "Start a recall from a Lot/Serial, discover impact, and prepare actions (scrap/returns).",
    "author": "PV Odoo",
    "license": "LGPL-3",
    "images": ["static/description/banner.png"],
    "depends": ["base", "product", "stock", "mail", "mrp"],
    "data": [
        "security/ir.model.access.csv",
        "security/recall_security.xml",
        "views/recall_views.xml",
        "views/stock_lot_views.xml",
        "data/recall_params.xml",
        "data/recall_params_phase3.xml",
        "data/quarantine_location.xml",
    ],
    "installable": True,
    "application": False,
}
