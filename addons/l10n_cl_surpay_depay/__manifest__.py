{
    "name": "Surpay Depay Integration",
    "summary": "Orquestacion de pagos headless para QR Depay",
    "version": "18.0.1.0.0",
    "category": "Accounting/Payment Providers",
    "author": "Surpay",
    "license": "LGPL-3",
    "depends": ["base", "surpay_base"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_config_parameter_data.xml",
        "data/ir_cron_data.xml",
        "views/payment_link_templates.xml",
    ],
    "installable": True,
    "application": False,
}
