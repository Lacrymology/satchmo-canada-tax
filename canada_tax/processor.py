from decimal import Decimal, getcontext, ROUND_HALF_UP
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from l10n.models import AdminArea, Country
from livesettings import config_value
from canada_tax.models import CanadianTaxRate as TaxRate
from product.models import TaxClass
from satchmo_store.contact.models import Contact
from satchmo_utils import is_string_like
import logging
import operator

log = logging.getLogger('tax.canada_tax')

class Processor(object):
    
    method = "area"
    
    def __init__(self, order=None, user=None):
        """
        Any preprocessing steps should go here
        For instance, copying the shipping and billing areas
        """
        self.order = order
        self.user = user
        
    def _get_location(self):
        area = None
        country = None
        
        calc_by_ship_address = bool(config_value('TAX','TAX_AREA_ADDRESS') == 'ship')
        
        if self.order:
            if calc_by_ship_address:
                country = self.order.ship_country
                area = self.order.ship_state
            else:
                country = self.order.bill_country
                area = self.order.bill_state
        
            if country:
                try:
                    country = Country.objects.get(iso2_code__exact=country)
                except Country.DoesNotExist:
                    log.error("Couldn't find Country from string: %s", country)
                    country = None
        elif self.user and self.user.is_authenticated():
            try:
                contact = Contact.objects.get(user=self.user)
                try:
                    if calc_by_ship_address:
                        area = contact.shipping_address.state
                    else:
                        area = contact.billing_address.state
                except AttributeError:
                    pass
                try:
                    if calc_by_ship_address:
                        country = contact.shipping_address.country
                    else:
                        country = contact.billing_address.country
                except AttributeError:
                    pass
            except Contact.DoesNotExist:
                pass

        if not country:
            from satchmo_store.shop.models import Config
            country = Config.objects.get_current().sales_country

        if area:
            try:
                area = AdminArea.objects.get(name__iexact=area,
                                             country=country)
            except AdminArea.DoesNotExist:
                try:
                    area = AdminArea.objects.get(abbrev__iexact=area,
                                                 country=country)
                except AdminArea.DoesNotExist:
                    log.info("Couldn't find AdminArea from string: %s", area)
                    area = None
        return area, country
    
    # taxclass: string, Default, Shipping
    # area: state/province
    # country: country!
    # get_object: return the rate object(s)
    def get_rate(self, taxclass=None, area=None, country=None, get_object=False, **kwargs):
        if not taxclass:
            taxclass = "Default"
        
        # initialize the rates array, allow for more than one rate
        rates = []
        
        # get the area/country
        if not (area or country):
            area, country = self._get_location()

        # get the taxclass object
        if is_string_like(taxclass):
            try:
                taxclass = TaxClass.objects.get(title__iexact=taxclass)
            except TaxClass.DoesNotExist:
                raise ImproperlyConfigured("Can't find a '%s' Tax Class", taxclass)
        
        # get all the rates for the given area
        if area:
            rates += list(TaxRate.objects.filter(taxClass=taxclass, taxZone=area))
        
        # get all rates for the given country
        if country:
            rates += list(TaxRate.objects.filter(taxClass=taxclass, taxCountry=country))

        log.debug("Got rates for taxclass: %s, area: %s, country: %s = [%s]", taxclass, area, country, rates)

        if get_object:
            return self.tax_rate_list(rates)
        else:
            return self.tax_rate_list(rates)[0]

    def get_percent(self, taxclass="Default", area=None, country=None):
        return 100 * self.get_rate(taxclass=taxclass, area=area, country=country)
    
    # given a list of TaxRate objects
    #   - try to split into compounded/non-compounded rates
    #   - order the compound rates properly
    #   - finalize all the rates such that the sum of each rate*price is
    #     the total tax
    #   - return total tax rate + list of taxCodes and individual rates
    #       (decimal('X,XX'), [('TAXCODE', Decimal('X.XX'))]
    def tax_rate_list(self, rates):
        if not rates:
            return (Decimal("0.00"), [])
        
        override_rates = [r for r in rates if r.override]
        if override_rates:
            # since this is a single overriding rate, it returns the first
            # override rate found, along with related rate data.
            rate = override_rates[0]
            return (rate.percentage, [(rate.taxCode, rate.percentage)])

        regular_rates = [r for r in rates if not r.compound]
        compound_rates = sorted([r for r in rates if r.compound],
                key=operator.attrgetter("compound_order"))
        
        totalrate = Decimal('0.00')
        receipt_data = []

        # adding / compounding tax rates
        for rate in regular_rates:
            receipt_data.append((rate.taxCode, rate.percentage))
            totalrate += rate.percentage
            
        for rate in compound_rates:
            percentage = rate.percentage + (rate.percentage * totalrate)
            receipt_data.append((rate.taxCode, percentage))
            totalrate += percentage
        
        return (totalrate, receipt_data)

    def by_price(self, taxclass, price):
        rate = self.get_rate(taxclass)
        return rate * price

    def by_product(self, product, quantity=Decimal('1')):
        """Get the tax for a given product"""
        price = product.get_qty_price(quantity)[0]
        tc = product.taxClass
        return self.by_price(tc, price)
        
    def by_orderitem(self, orderitem):
        if orderitem.product.taxable:
            price = orderitem.sub_total
            taxclass = orderitem.product.taxClass
            return self.by_price(taxclass, price)
        else:
            return Decimal("0.00")

    def shipping(self, subtotal=None, with_details=False):
        if subtotal is None and self.order:
            subtotal = self.order.shipping_sub_total

        tax_details = {}
        if subtotal:
            full_rate = None
            taxes = []
            if config_value('TAX','TAX_SHIPPING_CANADIAN'):
                try:
                    # get the tax class used for taxing shipping
                    taxclass = TaxClass.objects.get(title=config_value('TAX', 'TAX_CLASS'))
                    full_rate, taxes = self.get_rate(taxclass=taxclass, get_object=True)
                except ObjectDoesNotExist:
                    log.error("'Shipping' TaxClass doesn't exist.")

            if full_rate:
                ship_tax = full_rate * subtotal
            else:
                ship_tax = Decimal("0.00")

            if with_details:
                for taxcode, rate in taxes:
                    if taxcode not in tax_details:
                        tax_details[taxcode] = Decimal('0.00')
                    tax_details[taxcode] += rate * subtotal
            
        else:
            ship_tax = Decimal("0.00")
        
        if with_details:
            return ship_tax, tax_details
        else:
            return ship_tax

    def process(self, order=None):
        """
        Calculate the tax and return it.
        
        Probably need to make a breakout.
        """
        if order:
            self.order = order
        else:
            order = self.order
        
        sub_total = Decimal('0.00')
        tax_details = {}
        
        for item in order.orderitem_set.filter(product__taxable=True):
            # taxclass defaults to 'Default' if it's not set.
            taxclass = item.product.taxClass
            if taxclass:
                taxclass_key = taxclass.title
            else:
                taxclass_key = 'Default'

            # get_object=True makes us get the individual tax rates
            # along with the full rate.
            full_rate, taxes = self.get_rate(taxclass, get_object=True)

            # aggregate the subtotal using the full rate.
            price = item.sub_total
            sub_total += price * full_rate

            # aggregate tax details using individual rates.
            for taxcode, rate in taxes:
                if taxcode not in tax_details:
                    tax_details[taxcode] = Decimal('0.00')
                tax_details[taxcode] += rate * price
        
        ship_taxes, ship_tax_details = self.shipping(with_details=True)
        sub_total += ship_taxes

        if config_value("TAX", "TAX_SHIPPING_DETAILS_SEPARATE"):
            # Keep the shipping tax details separate from the merchandise taxes
            for taxcode in ship_tax_details:
                tax_details['Shipping ' + taxcode] = ship_tax_details['taxCode']
        else:
            # combine shipping and merchandise tax details together
            for taxcode in ship_tax_details:
                tax_details[taxcode] = (tax_details.get(taxcode, Decimal('0.00')) +
                        ship_tax_details[taxcode])
        
        return sub_total, tax_details

