# -*- coding: utf-8 -*-
from odoo import api, models, _

class StockLot(models.Model):
    _inherit = 'stock.lot'

    def action_start_recall(self):
        self.ensure_one()
        recall = self.env['recall.recall'].create({
            'name': _('Recall: %s') % (self.name,),
            'lot_ids': [(6, 0, [self.id])],
            'product_id': self.product_id.id,
        })
        # auto-discover endpoints on create
        recall.action_discover_endpoints()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'recall.recall',
            'view_mode': 'form',
            'res_id': recall.id,
        }
