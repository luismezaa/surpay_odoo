{
    "name": "Surpay Kushki Integration",
    "summary": "Integracion de pagos Kushki con terminales POS por cliente",
    "version": "18.0.1.0.0",
    "category": "Accounting/Payment Providers",
    "author": "Surpay",
    "license": "LGPL-3",
    "depends": ["base", "surpay_base", "l10n_cl_surpay_depay"],
    "data": [
        "security/ir.model.access.csv",
        "views/provider_config_views.xml"
    ],
    "installable": True,
    "application": False,
}
