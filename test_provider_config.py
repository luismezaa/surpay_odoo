import os
import sys
os.environ.setdefault('ODOO_RC', '/etc/odoo/odoo.conf')

import odoo
odoo.setup_odoo()

from odoo import _

# Import database context
from odoo.api import Environment
from odoo.sql_db import create_db
from odoo import registry

# Access the database
with registry.Registry.enter(odoo.registry('surpay_dev')).cursor() as cr:
    env = Environment(cr, odoo.SUPERUSER_ID, {})
    
    # Check if model exists
    config_model = env['surpay.provider.config']
    print("✓ Model 'surpay.provider.config' exists")
    print(f"  Fields: {list(config_model._fields.keys())[:5]}...")
    
    # Create a test record
    try:
        config = config_model.create({
            'provider': 'depay',
            'environment': 'sandbox',
            'api_key': 'test_key_123',
            'customer_uuid': 'cust-uuid-123',
            'pos_id': 'pos-123',
        })
        print(f"✓ Created provider config ID {config.id}")
        print(f"  Display: {config.display_name}")
    except Exception as e:
        print(f"✗ Error creating record: {e}")
        import traceback
        traceback.print_exc()
