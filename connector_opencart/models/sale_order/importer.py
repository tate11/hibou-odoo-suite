# © 2019 Hibou Corp.
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from copy import deepcopy, copy
from html import unescape

from odoo import fields, _
from odoo.addons.component.core import Component
from odoo.addons.connector.components.mapper import mapping
from odoo.exceptions import ValidationError


class SaleOrderBatchImporter(Component):
    _name = 'opencart.sale.order.batch.importer'
    _inherit = 'opencart.delayed.batch.importer'
    _apply_on = 'opencart.sale.order'

    def _import_record(self, external_id, store_id, job_options=None, **kwargs):
        if not job_options:
            job_options = {
                'max_retries': 0,
                'priority': 5,
            }
        if store_id is not None:
            store_binder = self.binder_for('opencart.store')
            store = store_binder.to_internal(store_id)
            user = store.sudo().warehouse_id.company_id.user_tech_id
            if user and user != self.env.user:
                # Note that this is a component, which has an env through it's 'colletion'
                # however, when importing the 'model' is actually what runs the delayed job
                env = self.env(user=user)
                self.collection.env = env
                self.model.env = env
        return super(SaleOrderBatchImporter, self)._import_record(
            external_id, job_options=job_options)

    def run(self, filters=None):
        """ Run the synchronization """
        if filters is None:
            filters = {}
        external_ids = list(self.backend_adapter.search(filters))
        for ids in external_ids:
            self._import_record(ids[0], ids[1])
        if external_ids:
            last_id = list(sorted(external_ids, key=lambda i: i[0]))[-1][0]
            self.backend_record.import_orders_after_id = last_id


class SaleOrderImportMapper(Component):
    _name = 'opencart.sale.order.mapper'
    _inherit = 'opencart.import.mapper'
    _apply_on = 'opencart.sale.order'

    direct = [('order_id', 'external_id'),
              ('store_id', 'store_id'),
              ]

    children = [('products', 'opencart_order_line_ids', 'opencart.sale.order.line'),
                ]

    def _add_coupon_lines(self, map_record, values):
        # Data from API
        # 'coupons': [{'amount': '7.68', 'code': '1111'}],
        record = map_record.source

        coupons = record.get('coupons')
        if not coupons:
            return values

        coupon_product = self.options.store.coupon_product_id or self.backend_record.coupon_product_id
        if not coupon_product:
            coupon_product = self.env.ref('connector_ecommerce.product_product_discount', raise_if_not_found=False)

        if not coupon_product:
            raise ValueError('Coupon %s on order requires coupon product in configuration.' % (coupons, ))
        for coupon in coupons:
            line_builder = self.component(usage='order.line.builder')
            line_builder.price_unit = -float(coupon.get('amount', 0.0))
            line_builder.product = coupon_product
            # `order.line.builder` does not allow naming.
            line_values = line_builder.get_line()
            code = coupon.get('code')
            if code:
                line_values['name'] = '%s Code: %s' % (coupon_product.name, code)
            values['order_line'].append((0, 0, line_values))
        return values

    def _add_shipping_line(self, map_record, values):
        record = map_record.source

        line_builder = self.component(usage='order.line.builder.shipping')
        line_builder.price_unit = record.get('shipping_exclude_tax', 0.0)

        if values.get('carrier_id'):
            carrier = self.env['delivery.carrier'].browse(values['carrier_id'])
            line_builder.product = carrier.product_id
            line = (0, 0, line_builder.get_line())
            values['order_line'].append(line)

        return values

    def finalize(self, map_record, values):
        values.setdefault('order_line', [])
        self._add_coupon_lines(map_record, values)
        self._add_shipping_line(map_record, values)
        values.update({
            'partner_id': self.options.partner_id,
            'partner_invoice_id': self.options.partner_invoice_id,
            'partner_shipping_id': self.options.partner_shipping_id,
        })
        onchange = self.component(
            usage='ecommerce.onchange.manager.sale.order'
        )
        # will I need more?!
        return onchange.play(values, values['opencart_order_line_ids'])

    @mapping
    def name(self, record):
        name = str(record['order_id'])
        prefix = self.options.store.sale_prefix or self.backend_record.sale_prefix
        if prefix:
            name = prefix + name
        return {'name': name}

    @mapping
    def date_order(self, record):
        return {'date_order': record.get('date_added', fields.Datetime.now())}

    @mapping
    def fiscal_position_id(self, record):
        fiscal_position = self.options.store.fiscal_position_id or self.backend_record.fiscal_position_id
        if fiscal_position:
            return {'fiscal_position_id': fiscal_position.id}

    @mapping
    def team_id(self, record):
        team = self.options.store.team_id or self.backend_record.team_id
        if team:
            return {'team_id': team.id}

    @mapping
    def payment_mode_id(self, record):
        record_method = record['payment_method']
        method = self.env['account.payment.mode'].search(
            [('name', '=', record_method)],
            limit=1,
        )
        if not method:
            raise ValueError('Payment Mode named "%s", cannot be found.' % (record_method, ))
        return {'payment_mode_id': method.id}

    @mapping
    def project_id(self, record):
        analytic_account = self.options.store.analytic_account_id or self.backend_record.analytic_account_id
        if analytic_account:
            return {'project_id': analytic_account.id}

    @mapping
    def warehouse_id(self, record):
        warehouse = self.options.store.warehouse_id or self.backend_record.warehouse_id
        if warehouse:
            return {'warehouse_id': warehouse.id}

    @mapping
    def shipping_code(self, record):
        method = record.get('shipping_code') or record.get('shipping_method')
        if not method:
            return {'carrier_id': False}

        carrier_domain = [('opencart_code', '=', method.strip())]
        company = self.options.store.company_id or self.backend_record.company_id
        if company:
            carrier_domain += [
                '|', ('company_id', '=', company.id), ('company_id', '=', False)
            ]
        carrier = self.env['delivery.carrier'].search(carrier_domain, limit=1)
        if not carrier:
            raise ValueError('Delivery Carrier for method Code "%s", cannot be found.' % (method, ))
        return {'carrier_id': carrier.id}

    @mapping
    def company_id(self, record):
        company = self.options.store.company_id or self.backend_record.company_id
        if not company:
            raise ValidationError('Company not found in Opencart Backend or Store')
        return {'company_id': company.id}

    @mapping
    def backend_id(self, record):
        return {'backend_id': self.backend_record.id}

    @mapping
    def total_amount(self, record):
        total_amount = record['total']
        return {'total_amount': total_amount}


class SaleOrderImporter(Component):
    _name = 'opencart.sale.order.importer'
    _inherit = 'opencart.importer'
    _apply_on = 'opencart.sale.order'

    def _must_skip(self):
        if self.binder.to_internal(self.external_id):
            return _('Already imported')

    def _before_import(self):
        # Check if status is ok, etc. on self.opencart_record
        pass

    def _create_partner(self, values):
        return self.env['res.partner'].create(values)

    def _partner_matches(self, partner, values):
        for key, value in values.items():
            if key in ('active', 'parent_id', 'type'):
                continue

            if key == 'state_id':
                if value != partner.state_id.id:
                    return False
            elif key == 'country_id':
                if value != partner.country_id.id:
                    return False
            elif bool(value) and value != getattr(partner, key):
                return False
        return True

    def _make_partner_name(self, firstname, lastname):
        name = (str(firstname or '').strip() + ' ' + str(lastname or '').strip()).strip()
        if not name:
            return 'Undefined'
        return name

    def _get_partner_values(self, info_string='shipping_'):
        record = self.opencart_record

        # find or make partner with these details.
        email = record.get('email')
        if not email:
            raise ValueError('Order does not have email in : ' + str(record))

        phone = record.get('telephone', False)

        info = {}
        for k, v in record.items():
            # Strip the info_string so that the remainder of the code depends on it.
            if k.find(info_string) == 0:
                info[k[len(info_string):]] = v


        name = self._make_partner_name(info.get('firstname', ''), info.get('lastname', ''))
        street = info.get('address_1', '')
        street2 = info.get('address_2', '')
        city = info.get('city', '')
        state_code = info.get('zone_code', '')
        zip_ = info.get('postcode', '')
        country_code = info.get('iso_code_2', '')
        country = self.env['res.country'].search([('code', '=', country_code)], limit=1)
        state = self.env['res.country.state'].search([
            ('country_id', '=', country.id),
            ('code', '=', state_code)
        ], limit=1)

        return {
            'email': email.strip(),
            'name': name.strip(),
            'phone': phone.strip(),
            'street': street.strip(),
            'street2': street2.strip(),
            'zip': zip_.strip(),
            'city': city.strip(),
            'state_id': state.id,
            'country_id': country.id,
        }

    def _import_addresses(self):
        partner_values = self._get_partner_values()
        partners = self.env['res.partner'].search([
            ('email', '=', partner_values['email']),
            '|', ('active', '=', False), ('active', '=', True),
        ], order='active DESC, id ASC')

        partner = None
        for possible in partners:
            if self._partner_matches(possible, partner_values):
                partner = possible
                break
        if not partner and partners:
            partner = partners[0]

        if not partner:
            # create partner.
            partner = self._create_partner(copy(partner_values))

        if not self._partner_matches(partner, partner_values):
            partner_values['parent_id'] = partner.id
            shipping_values = copy(partner_values)
            shipping_values['type'] = 'delivery'
            shipping_partner = self._create_partner(shipping_values)
        else:
            shipping_partner = partner

        invoice_values = self._get_partner_values(info_string='payment_')
        invoice_values['type'] = 'invoice'

        if (not self._partner_matches(partner, invoice_values)
                and not self._partner_matches(shipping_partner, invoice_values)):
            # Try to find existing invoice address....
            for possible in partners:
                if self._partner_matches(possible, invoice_values):
                    invoice_partner = possible
                    break
            else:
                invoice_values['parent_id'] = partner.id
                invoice_partner = self._create_partner(copy(invoice_values))
        elif self._partner_matches(partner, invoice_values):
            invoice_partner = partner
        elif self._partner_matches(shipping_partner, invoice_values):
            invoice_partner = shipping_partner

        self.partner = partner
        self.shipping_partner = shipping_partner
        self.invoice_partner = invoice_partner

    def _check_special_fields(self):
        assert self.partner, (
            "self.partner should have been defined "
            "in SaleOrderImporter._import_addresses")
        assert self.shipping_partner, (
            "self.shipping_partner should have been defined "
            "in SaleOrderImporter._import_addresses")
        assert self.invoice_partner, (
            "self.invoice_partner should have been defined "
            "in SaleOrderImporter._import_addresses")

    def _get_store(self, record):
        store_binder = self.binder_for('opencart.store')
        return store_binder.to_internal(record['store_id'])

    def _create_data(self, map_record, **kwargs):
        # non dependencies
        # our current handling of partners doesn't require anything special for the store
        self._check_special_fields()
        store = self._get_store(map_record.source)
        return super(SaleOrderImporter, self)._create_data(
            map_record,
            partner_id=self.partner.id,
            partner_invoice_id=self.invoice_partner.id,
            partner_shipping_id=self.shipping_partner.id,
            store=store,
            **kwargs
        )

    def _create(self, data):
        binding = super(SaleOrderImporter, self)._create(data)
        # Without this, it won't map taxes with the fiscal position.
        if binding.fiscal_position_id:
            binding.odoo_id._compute_tax_id()

        return binding

    def _import_dependencies(self):
        record = self.opencart_record
        self._import_addresses()
        for product in record.get('products', []):
            if 'product_id' in product and product['product_id']:
                self._import_dependency(product['product_id'], 'opencart.product.template')


class SaleOrderLineImportMapper(Component):

    _name = 'opencart.sale.order.line.mapper'
    _inherit = 'opencart.import.mapper'
    _apply_on = 'opencart.sale.order.line'

    direct = [('quantity', 'product_uom_qty'),
              ('price', 'price_unit'),
              ('order_product_id', 'external_id'),
              ]

    @mapping
    def name(self, record):
        return {'name': unescape(record['name'])}

    @mapping
    def product_id(self, record):
        product_id = record['product_id']
        binder = self.binder_for('opencart.product.template')
        # do not unwrap, because it would be a product.template, but I need a specific variant
        opencart_product_template = binder.to_internal(product_id, unwrap=False)
        if record.get('option'):
            product = opencart_product_template.opencart_sale_get_combination(record.get('option'))
        else:
            product = opencart_product_template.odoo_id.product_variant_id
        return {'product_id': product.id, 'product_uom': product.uom_id.id}
