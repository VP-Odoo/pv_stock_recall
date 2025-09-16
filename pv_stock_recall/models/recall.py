# -*- coding: utf-8 -*-
from collections import defaultdict
from datetime import datetime
from odoo import api, fields, models, _
from odoo.exceptions import UserError

ENDPOINT_SELECTION = [
    ('onhand', 'On-hand'),
    ('delivered', 'Delivered to Customer'),
]

ACTION_SELECTION = [
    ('scrap', 'Scrap'),
    ('rma', 'Return (RMA)'),
    ('none', 'No Action'),
]


class Recall(models.Model):
    _name = 'recall.recall'
    _description = 'Product Recall'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(default=lambda self: _('Recall'), tracking=True)
    state = fields.Selection(
        [('draft', 'Draft'), ('in_progress', 'In Progress'), ('closed', 'Closed'), ('cancel', 'Cancelled')],
        default='draft', tracking=True
    )
    product_id = fields.Many2one('product.product', string='Product', tracking=True)
    lot_ids = fields.Many2many('stock.lot', string='Lots/Serials', required=True, tracking=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company, required=True, index=True)
    reason = fields.Text()
    severity = fields.Selection([('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')], default='medium')
    line_ids = fields.One2many('recall.line', 'recall_id', string='Endpoints')
    deadline = fields.Datetime()
    impact_summary = fields.Text(readonly=True)

    # KPIs (validated quantities)
    kpi_scrap_qty = fields.Float(string='Scrapped Qty', compute='_compute_kpis', digits='Product Unit of Measure')
    kpi_return_qty = fields.Float(string='Returned Qty', compute='_compute_kpis', digits='Product Unit of Measure')

    # Smart button counters (documents regardless of state)
    smart_scrap_count = fields.Integer(string='Scraps', compute='_compute_counts')
    smart_return_count = fields.Integer(string='Return Pickings', compute='_compute_counts')

    can_generate = fields.Boolean(compute='_compute_can_generate')

    # ---------------- KPI / counters ----------------
    def _compute_can_generate(self):
        for rec in self:
            rec.can_generate = bool(rec.line_ids)

    def _compute_kpis(self):
        for rec in self:
            scrap_total = 0.0
            return_total = 0.0
            for line in rec.line_ids:
                if line.scrap_id and getattr(line.scrap_id, 'state', 'done') == 'done':
                    scrap_total += line.quantity or 0.0
                if line.return_picking_id and line.return_picking_id.state == 'done':
                    return_total += line.quantity or 0.0
            rec.kpi_scrap_qty = scrap_total
            rec.kpi_return_qty = return_total

    def _compute_counts(self):
        for rec in self:
            rec.smart_scrap_count = len({sid for sid in rec.line_ids.mapped('scrap_id.id') if sid})
            rec.smart_return_count = len({pid for pid in rec.line_ids.mapped('return_picking_id.id') if pid})

    # ---------------- Overlap guard ----------------
    def _raise_if_lot_conflicts(self):
        active_states = ['draft', 'in_progress']
        for rec in self:
            if not rec.lot_ids:
                continue
            conflicts = self.search([
                ('id', '!=', rec.id),
                ('company_id', '=', rec.company_id.id),
                ('state', 'in', active_states),
                ('lot_ids', 'in', rec.lot_ids.ids),
            ])
            if conflicts:
                lines = []
                for lot in rec.lot_ids:
                    recs_for_lot = conflicts.filtered(lambda r: lot in r.lot_ids)
                    if recs_for_lot:
                        names = ", ".join(recs_for_lot.mapped('name'))
                        lines.append("%s → %s" % (lot.display_name or lot.name, names))
                details = "\n".join(lines) if lines else ", ".join(conflicts.mapped('name'))
                raise UserError(
                    _("Some selected Lots/Serials are already part of another active recall.\n\nConflicts:\n%s\n\n"
                      "Close/Cancel the other recall or remove those lots before continuing.") % details
                )

    # --------- tiny config helpers ---------
    @api.model
    def _get_param_bool(self, key, default=False):
        val = self.env['ir.config_parameter'].sudo().get_param(key, default and '1' or '0')
        return str(val).lower() in ('1', 'true', 't', 'y', 'yes')

    # ---- Phase 3: helpers (params) ----
    @api.model
    def _p3_block_reservations(self):
        return self._get_param_bool('pv_stock_recall.block_reservations', default=True)

    @api.model
    def _p3_use_quarantine(self):
        return self._get_param_bool('pv_stock_recall.use_quarantine', default=False)

    def _p3_quarantine_location(self):
        loc = self.env.ref('pv_stock_recall.stock_location_recall_quarantine', raise_if_not_found=False)
        return loc

    # ---- Phase 3: unreserve existing pickings when recall becomes active / changes ----
    def _phase3_unreserve_open_pickings(self):
        if not self._p3_block_reservations():
            return
        Move = self.env['stock.move'].sudo()
        for rec in self:
            lot_ids = rec.lot_ids.ids
            if not lot_ids:
                continue
            moves = Move.search([
                ('state', 'in', ('confirmed', 'assigned')),
                ('company_id', '=', rec.company_id.id),
                ('picking_id.state', 'not in', ('done', 'cancel')),
                ('move_line_ids.lot_id', 'in', lot_ids),
            ])
            if not moves:
                continue
            pickings = moves.mapped('picking_id')
            for p in pickings:
                to_unreserve = moves.filtered(lambda m: m.picking_id.id == p.id)
                if to_unreserve:
                    to_unreserve._do_unreserve()
                    lots_text = ", ".join(sorted({ml.lot_id.display_name for ml in to_unreserve.mapped('move_line_ids') if ml.lot_id}))
                    p.message_post(body=_("Unreserved because an active Recall (%s) affects lots: %s")
                                         % (rec.display_name, lots_text or "-"))

    @api.model
    def create(self, vals):
        rec = super().create(vals)
        rec._raise_if_lot_conflicts()
        rec._phase3_unreserve_open_pickings()
        return rec

    def write(self, vals):
        res = super().write(vals)
        self._raise_if_lot_conflicts()
        if 'state' in vals or 'lot_ids' in vals or 'line_ids' in vals:
            active_recs = self.filtered(lambda r: r.state in ('draft', 'in_progress'))
            if active_recs:
                active_recs._phase2_notify_impacted_mos()
                active_recs._phase3_unreserve_open_pickings()
        return res

    # ---------------- Helpers ----------------
    def _find_related_return_picking(self, picking):
        return self.env['stock.picking'].search([
            ('move_ids_without_package.origin_returned_move_id', 'in', picking.move_ids_without_package.ids),
        ], order='id desc', limit=1)

    def _remaining_to_return(self, picking, product_id=None, lot_id=None):
        MoveLine = self.env['stock.move.line']
        dom_out = [
            ('picking_id', '=', picking.id),
            ('state', '=', 'done'),
            ('location_dest_id.usage', '=', 'customer'),
        ]
        if product_id:
            dom_out.append(('product_id', '=', product_id))
        if lot_id:
            dom_out.append(('lot_id', '=', lot_id))
        delivered_qty = sum(ml.quantity for ml in MoveLine.search(dom_out))

        returned_qty = 0.0
        returned_pickings = self.env['stock.picking'].search([
            ('move_ids_without_package.origin_returned_move_id', 'in', picking.move_ids_without_package.ids),
            ('state', '!=', 'cancel'),
        ])
        if returned_pickings:
            dom_ret = [
                ('picking_id', 'in', returned_pickings.ids),
                ('state', '=', 'done'),
            ]
            if product_id:
                dom_ret.append(('product_id', '=', product_id))
            if lot_id:
                dom_ret.append(('lot_id', '=', lot_id))
            returned_qty = sum(ml.quantity for ml in MoveLine.search(dom_ret))

        remaining = delivered_qty - returned_qty
        return remaining if remaining > 0 else 0.0

    def _remaining_onhand(self, location_id, product_id, lot_id):
        Quant = self.env['stock.quant']
        qty = sum(Quant.search([
            ('company_id', '=', self.company_id.id),
            ('location_id', '=', location_id),
            ('location_id.usage', '=', 'internal'),
            ('product_id', '=', product_id),
            ('lot_id', '=', lot_id),
        ]).mapped('quantity'))
        return qty if qty > 0 else 0.0

    def _auto_close_if_done(self):
        for rec in self:
            if rec.state != 'closed' and rec.line_ids and all(l.state == 'done' for l in rec.line_ids):
                rec.state = 'closed'
                rec.message_post(body=_("All endpoints processed → Recall closed."))

    def _qty_returned_now(self, return_picking, delivery_picking, product_id, lot_id=False):
        qty = 0.0
        moves = return_picking.move_ids_without_package.filtered(
            lambda m: m.product_id.id == product_id and
                      m.origin_returned_move_id.id in delivery_picking.move_ids_without_package.ids
        )
        if lot_id:
            for ml in moves.mapped('move_line_ids'):
                if ml.state == 'done' and ml.product_id.id == product_id and ml.lot_id and ml.lot_id.id == lot_id:
                    qty += ml.quantity
        if qty <= 1e-9 and return_picking.state == 'done':
            for mv in moves:
                if mv.state == 'done':
                    if mv.move_line_ids:
                        qty += sum(ml.quantity for ml in mv.move_line_ids if ml.state == 'done')
                    else:
                        qty += mv.quantity
        return qty

    # --------- MRP integration helpers ---------
    def _mrp_finished_lots_from_component_lots(self, lots):
        result = {}
        MoveLine = self.env['stock.move.line']
        consumed = MoveLine.search([
            ('lot_id', 'in', lots.ids),
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'done'),
            ('move_id.raw_material_production_id', '!=', False),
        ])
        mo_ids = {ml.move_id.raw_material_production_id.id for ml in consumed if ml.move_id and ml.move_id.raw_material_production_id}
        if not mo_ids:
            return result
        finished_mls = MoveLine.search([
            ('move_id.production_id', 'in', list(mo_ids)),
            ('state', '=', 'done'),
            ('lot_id', '!=', False),
        ])
        for ml in finished_mls:
            mo = ml.move_id.production_id
            if ml.lot_id and mo:
                result[ml.lot_id.id] = mo.id
        return result

    def _phase2_impacted_open_mos(self):
        Production = self.env['mrp.production'].sudo()
        MoveLine = self.env['stock.move.line'].sudo()
        impacted = Production.browse()
        include_downstream = self._get_param_bool('pv_stock_recall.include_downstream', default=False)

        for rec in self:
            impacted |= Production.browse(rec.line_ids.mapped('production_id').ids)

            if rec.lot_ids:
                cons = MoveLine.search([
                    ('lot_id', 'in', rec.lot_ids.ids),
                    ('move_id.raw_material_production_id', '!=', False),
                    ('state', '!=', 'cancel'),
                    ('company_id', '=', rec.company_id.id),
                ])
                impacted |= cons.mapped('move_id.raw_material_production_id')

            if include_downstream:
                aff_lots = rec.line_ids.mapped('lot_id')
                if aff_lots:
                    cons2 = MoveLine.search([
                        ('lot_id', 'in', aff_lots.ids),
                        ('move_id.raw_material_production_id', '!=', False),
                        ('state', '!=', 'cancel'),
                        ('company_id', '=', rec.company_id.id),
                    ])
                    impacted |= cons2.mapped('move_id.raw_material_production_id')

        return impacted.filtered(lambda mo: mo.state not in ('done', 'cancel'))

    def _phase2_notify_impacted_mos(self):
        mos = self._phase2_impacted_open_mos()
        if not mos:
            return
        todo_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        if not todo_type:
            return
        for rec in self:
            for mo in mos:
                existing = mo.activity_ids.filtered(
                    lambda a: a.activity_type_id.id == todo_type.id and a.summary == ('Recall %s' % rec.name)
                )
                if existing:
                    continue
                user_id = mo.user_id.id or self.env.user.id
                if hasattr(mo, 'activity_schedule'):
                    mo.activity_schedule(
                        activity_type_id=todo_type.id,
                        summary='Recall %s' % rec.name,
                        note=_('This MO is impacted by Recall %s.') % rec.name,
                        user_id=user_id,
                    )
                else:
                    self.env['mail.activity'].sudo().create({
                        'activity_type_id': todo_type.id,
                        'res_model_id': self.env['ir.model']._get_id('mrp.production'),
                        'res_id': mo.id,
                        'user_id': user_id,
                        'summary': 'Recall %s' % rec.name,
                        'note': _('This MO is impacted by Recall %s.') % rec.name,
                    })

    # ---------------- Discovery ----------------
    def action_discover_endpoints(self):
        self.ensure_one()
        if not self.lot_ids:
            raise UserError(_('Please select at least one Lot/Serial.'))

        self.line_ids.unlink()

        fg_map = self._mrp_finished_lots_from_component_lots(self.lot_ids)  # {lot_id: mo_id}
        all_lot_ids = set(self.lot_ids.ids) | set(fg_map.keys())

        # --- ON-HAND ---
        Quant = self.env['stock.quant']
        quants = Quant.search([
            ('lot_id', 'in', list(all_lot_ids)),
            ('quantity', '>', 0),
            ('location_id.usage', '=', 'internal'),
            ('company_id', '=', self.company_id.id),
        ])
        grouped_onhand = defaultdict(lambda: {'qty': 0.0, 'product_id': False})
        for q in quants:
            if q.lot_id and q.location_id:
                key = (q.lot_id.id, q.location_id.id)
                grouped_onhand[key]['qty'] += q.quantity or 0.0
                grouped_onhand[key]['product_id'] = q.product_id.id or grouped_onhand[key]['product_id']

        for (lot_id, location_id), data in grouped_onhand.items():
            qty = data['qty']
            if qty <= 0:
                continue
            product_id = data['product_id'] or self.env['stock.lot'].browse(lot_id).product_id.id
            self.env['recall.line'].create({
                'recall_id': self.id,
                'endpoint_type': 'onhand',
                'product_id': product_id,
                'lot_id': lot_id,
                'location_id': location_id,
                'quantity': qty,
                'proposed_action': 'scrap',
                'production_id': fg_map.get(lot_id) or False,
            })

        # --- DELIVERED (only remaining-to-return) ---
        MoveLine = self.env['stock.move.line']
        delivered_mls = MoveLine.search([
            ('lot_id', 'in', list(all_lot_ids)),
            ('quantity', '>', 0),
            ('state', '=', 'done'),
            ('location_dest_id.usage', '=', 'customer'),
            ('company_id', '=', self.company_id.id),
        ])
        by_picking = defaultdict(lambda: {'qty': 0.0, 'lot_id': False, 'product_id': False, 'partner_id': False})
        for ml in delivered_mls:
            if ml.picking_id:
                e = by_picking[ml.picking_id.id]
                e['qty'] += ml.quantity or 0.0
                e['lot_id'] = ml.lot_id.id or e['lot_id']
                e['product_id'] = ml.product_id.id or e['product_id']
                e['partner_id'] = ml.picking_id.partner_id.id or e['partner_id']

        for picking_id, info in by_picking.items():
            picking = self.env['stock.picking'].browse(picking_id)
            remaining = self._remaining_to_return(picking, product_id=info['product_id'], lot_id=info['lot_id'])
            if remaining <= 0:
                continue
            self.env['recall.line'].create({
                'recall_id': self.id,
                'endpoint_type': 'delivered',
                'product_id': info['product_id'],
                'lot_id': info['lot_id'],
                'picking_id': picking_id,
                'partner_id': info['partner_id'],
                'quantity': remaining,
                'proposed_action': 'rma',
                'production_id': fg_map.get(info['lot_id']) or False,
            })

        # Summary
        onhand_total = sum(self.line_ids.filtered(lambda l: l.endpoint_type == 'onhand').mapped('quantity')) or 0.0
        delivered_total = sum(self.line_ids.filtered(lambda l: l.endpoint_type == 'delivered').mapped('quantity')) or 0.0
        self.impact_summary = _('On-hand: %(onhand).2f | Delivered (to recall): %(deliv).2f') % {
            'onhand': onhand_total, 'deliv': delivered_total
        }

        if not self.product_id:
            products = set(self.line_ids.mapped('product_id').ids)
            if len(products) == 1:
                self.product_id = self.env['product.product'].browse(list(products)[0])

        if self.state == 'draft':
            self.state = 'in_progress'

        # Phase 3: proactive unreserve
        self._phase3_unreserve_open_pickings()
        return True

    # ---------------- Generation ----------------
    def action_generate_actions(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_('No endpoints found. Click "Discover Endpoints" first.'))

        Scrap = self.env['stock.scrap']
        created_scraps = 0
        created_returns = 0
        scrap_qty_field = 'quantity' if 'quantity' in Scrap._fields else 'scrap_qty'

        # ---------- SCRAPS ----------
        for line in self.line_ids.filtered(lambda l: l.endpoint_type == 'onhand' and l.proposed_action == 'scrap' and l.state == 'todo'):
            remaining = self._remaining_onhand(line.location_id.id, line.product_id.id, line.lot_id.id)
            if remaining <= 0.0:
                existing = self.env['stock.scrap'].search([
                    ('company_id', '=', self.company_id.id),
                    ('product_id', '=', line.product_id.id),
                    ('lot_id', '=', line.lot_id.id),
                    ('location_id', '=', line.location_id.id),
                    ('state', '!=', 'cancel'),
                ], order='id desc', limit=1)
                if existing:
                    qty_done = getattr(existing, scrap_qty_field, getattr(existing, 'quantity', 0.0)) or 0.0
                    if qty_done > 1e-9:
                        self.env['recall.line'].create({
                            'recall_id': line.recall_id.id,
                            'endpoint_type': line.endpoint_type,
                            'proposed_action': line.proposed_action,
                            'state': 'done',
                            'product_id': line.product_id.id,
                            'lot_id': line.lot_id.id,
                            'quantity': min(line.quantity or 0.0, qty_done),
                            'location_id': line.location_id.id,
                            'scrap_id': existing.id,
                            'production_id': line.production_id.id or False,
                        })
                line.state = 'done'
                continue

            if line.scrap_id and getattr(line.scrap_id, 'state', 'draft') != 'done':
                continue

            # IMPORTANT: pass a context flag so Quant._gather doesn't block this scrap
            scrap = Scrap.with_context(pv_ignore_recall_block=True).create({
                'product_id': line.product_id.id,
                'lot_id': line.lot_id.id,
                scrap_qty_field: min(remaining, line.quantity or remaining),
                'location_id': line.location_id.id,
                'company_id': self.company_id.id,
                'name': _('Recall %s') % (self.display_name,),
            })
            line.scrap_id = scrap.id
            created_scraps += 1

        # ---------- RETURNS ----------
        ReturnWizard = self.env['stock.return.picking']
        for line in self.line_ids.filtered(lambda l: l.endpoint_type == 'delivered' and l.proposed_action == 'rma' and l.state == 'todo' and l.picking_id):
            picking = line.picking_id
            remaining = self._remaining_to_return(picking, product_id=line.product_id.id, lot_id=line.lot_id.id)
            if remaining <= 0.0:
                candidate = self._find_related_return_picking(picking)
                if candidate and (line.quantity or 0.0) > 1e-9:
                    self.env['recall.line'].create({
                        'recall_id': line.recall_id.id,
                        'endpoint_type': line.endpoint_type,
                        'proposed_action': line.proposed_action,
                        'state': 'done',
                        'product_id': line.product_id.id,
                        'lot_id': line.lot_id.id,
                        'quantity': line.quantity or 0.0,
                        'partner_id': line.partner_id.id if line.partner_id else False,
                        'picking_id': line.picking_id.id,
                        'return_picking_id': candidate.id,
                        'production_id': line.production_id.id or False,
                    })
                line.state = 'done'
                continue

            new_picking = self.env['stock.picking']
            try:
                wizard = ReturnWizard.with_context(active_model='stock.picking', active_id=picking.id).create({})
                if hasattr(wizard, '_onchange_picking_id'):
                    wizard._onchange_picking_id()
                if getattr(wizard, 'product_return_moves', False):
                    for wline in wizard.product_return_moves:
                        if wline.move_id and wline.move_id.product_id.id == line.product_id.id:
                            wline.quantity = line.quantity
                        else:
                            wline.quantity = 0.0
                action = False
                if hasattr(wizard, 'action_create_returns_all'):
                    action = wizard.action_create_returns_all()
                elif hasattr(wizard, 'action_create_returns'):
                    action = wizard.action_create_returns()
                elif hasattr(wizard, 'create_returns'):
                    action = wizard.create_returns()
                new_id = False
                if isinstance(action, dict):
                    new_id = action.get('res_id') or (action.get('res_ids')[0] if action.get('res_ids') else False)
                if not new_id:
                    candidate = self._find_related_return_picking(picking)
                    if candidate:
                        new_id = candidate.id
                if new_id:
                    new_picking = self.env['stock.picking'].browse(new_id)
            except Exception as e:
                self.message_post(body=_("Return could not be generated for %s: %s") % (picking.display_name, e))

            if new_picking and new_picking.exists():
                if self._p3_use_quarantine():
                    qloc = self._p3_quarantine_location()
                    if qloc:
                        new_picking.write({'location_dest_id': qloc.id})
                        new_picking.move_ids_without_package.write({'location_dest_id': qloc.id})
                        new_picking.move_line_ids.write({'location_dest_id': qloc.id})
                new_picking.recall_line_id = line.id
                line.return_picking_id = new_picking.id
                created_returns += 1

        self._auto_close_if_done()

        if created_scraps == 0 and created_returns == 0:
            return {'type': 'ir.actions.client', 'tag': 'display_notification',
                    'params': {'title': _('No actions created'),
                               'message': _('Nothing left to scrap/return or actions already in progress.'), 'sticky': False}}
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    # ---------------- Smart buttons ----------------
    def action_open_scraps(self):
        self.ensure_one()
        ids = [s.id for s in self.line_ids.mapped('scrap_id') if s]
        return {
            'type': 'ir.actions.act_window', 'name': _('Scraps'),
            'res_model': 'stock.scrap', 'view_mode': 'list,form',
            'domain': [('id', 'in', ids)], 'target': 'current'
        }

    def action_open_returns(self):
        self.ensure_one()
        ids = [p.id for p in self.line_ids.mapped('return_picking_id') if p]
        return {
            'type': 'ir.actions.act_window', 'name': _('Return Pickings'),
            'res_model': 'stock.picking', 'view_mode': 'list,form',
            'domain': [('id', 'in', ids)], 'target': 'current'
        }

    def action_close(self):
        self.ensure_one()
        self.state = 'closed'

    # ---------------- Trace graph data ----------------
    def get_trace_graph_data(self):
        """Return nodes & edges (SVG-agnostic) for an interactive graph of all
        transactions involving this recall's affected lots.

        Nodes:
            - loc:<id>     (stock.location)
            - pick:<id>    (stock.picking)
            - mo:<id>      (mrp.production)
            - scrap:<id>   (stock.scrap)
        Edges (directed):
            - loc -> pick -> loc
            - loc -> mo   (raw consumption)
            - mo  -> loc  (finished production)
            - loc -> scrap
        """
        self.ensure_one()
        company_id = self.company_id.id

        # lots included in the trace
        lots = self.lot_ids
        # optionally include finished lots derived from recalled component lots
        if self.env['ir.config_parameter'].sudo().get_param('pv_stock_recall.include_downstream', '0') in ('1', 'true', 'True'):
            fin_map = self._mrp_finished_lots_from_component_lots(lots)
            if fin_map:
                lots = lots | self.env['stock.lot'].browse(list(fin_map.keys()))

        lot_ids = lots.ids or []
        res_nodes = {}  # key -> node
        edges = []      # list of edges

        def add_node(key, label, type_, model, rec_id):
            if key not in res_nodes:
                res_nodes[key] = {'key': key, 'label': label, 'type': type_, 'model': model, 'res_id': rec_id}

        def add_edge(src_key, dst_key, label, qty, lot_name, date_iso, model=None, res_id=None):
            edges.append({
                'source': src_key, 'target': dst_key, 'label': label,
                'qty': qty, 'lot': lot_name, 'date': date_iso,
                'model': model, 'res_id': res_id,
            })

        MoveLine = self.env['stock.move.line'].sudo()
        Scrap = self.env['stock.scrap'].sudo()

        lot_names = {l.id: (l.display_name or l.name) for l in lots}

        # ---- STOCK MOVE LINES (everything related to those lots, except cancel) ----
        if lot_ids:
            mls = MoveLine.search([
                ('company_id', '=', company_id),
                ('state', '!=', 'cancel'),
                ('lot_id', 'in', lot_ids),
            ], order='date,id')

            for ml in mls:
                qty = float(getattr(ml, 'quantity', 0.0) or getattr(ml, 'qty_done', 0.0) or 0.0)
                if qty <= 1e-9:
                    continue

                src = ml.location_id
                dst = ml.location_dest_id
                if src:
                    add_node(f'loc:{src.id}', src.complete_name or src.display_name, 'location', 'stock.location', src.id)
                if dst:
                    add_node(f'loc:{dst.id}', dst.complete_name or dst.display_name, 'location', 'stock.location', dst.id)

                lot_label = lot_names.get(ml.lot_id.id) if ml.lot_id else ''
                date_iso = (ml.date or ml.write_date or datetime.utcnow()).strftime('%Y-%m-%d %H:%M:%S')

                # Picking flow
                if ml.picking_id:
                    p = ml.picking_id
                    ptype_label = p.picking_type_id.name or getattr(p, 'picking_type_code', False) or 'Picking'
                    add_node(f'pick:{p.id}', p.name or p.display_name, 'picking', 'stock.picking', p.id)
                    # src -> picking
                    if src:
                        add_edge(f'loc:{src.id}', f'pick:{p.id}', ptype_label,
                                 qty, lot_label, date_iso, 'stock.picking', p.id)
                    # picking -> dst
                    if dst:
                        add_edge(f'pick:{p.id}', f'loc:{dst.id}', ptype_label,
                                 qty, lot_label, date_iso, 'stock.picking', p.id)
                    continue

                # MRP raw consumption
                if ml.move_id and ml.move_id.raw_material_production_id:
                    mo = ml.move_id.raw_material_production_id
                    add_node(f'mo:{mo.id}', mo.name or mo.display_name, 'mo', 'mrp.production', mo.id)
                    if src:
                        add_edge(f'loc:{src.id}', f'mo:{mo.id}', 'MO Consume', qty, lot_label, date_iso, 'mrp.production', mo.id)
                    continue

                # MRP finished production
                if ml.move_id and ml.move_id.production_id:
                    mo = ml.move_id.production_id
                    add_node(f'mo:{mo.id}', mo.name or mo.display_name, 'mo', 'mrp.production', mo.id)
                    if dst:
                        add_edge(f'mo:{mo.id}', f'loc:{dst.id}', 'MO Produce', qty, lot_label, date_iso, 'mrp.production', mo.id)
                    continue

                # Generic move (without picking/MO) — connect locations directly
                if src and dst:
                    add_edge(f'loc:{src.id}', f'loc:{dst.id}', 'Move', qty, lot_label, date_iso, 'stock.move', ml.move_id.id if ml.move_id else False)

        # ---- SCRAPS explicitly (they create internal moves, but we expose the doc, too) ----
        if lot_ids:
            scraps = Scrap.search([
                ('company_id', '=', company_id),
                ('state', '!=', 'cancel'),
                ('lot_id', 'in', lot_ids),
            ], order='create_date,id')
            for sc in scraps:
                add_node(f'scrap:{sc.id}', sc.name or sc.display_name or _('Scrap'), 'scrap', 'stock.scrap', sc.id)
                loc = sc.location_id
                if loc:
                    add_node(f'loc:{loc.id}', loc.complete_name or loc.display_name, 'location', 'stock.location', loc.id)
                    lot_label = lot_names.get(sc.lot_id.id) if sc.lot_id else ''
                    date_iso = (sc.date_done or sc.create_date or datetime.utcnow()).strftime('%Y-%m-%d %H:%M:%S')
                    qty = float(getattr(sc, 'quantity', False) or getattr(sc, 'scrap_qty', 0.0) or 0.0)
                    if qty > 1e-9:
                        add_edge(f'loc:{loc.id}', f'scrap:{sc.id}', 'Scrap', qty, lot_label, date_iso, 'stock.scrap', sc.id)

        # Prepare result
        nodes = list(res_nodes.values())
        meta = {
            'recall': {'id': self.id, 'name': self.display_name},
            'lots': [{'id': l.id, 'name': lot_names[l.id]} for l in lots],
            'counts': {'nodes': len(nodes), 'edges': len(edges)},
        }
        return {'nodes': nodes, 'edges': edges, 'meta': meta}


class RecallLine(models.Model):
    _name = 'recall.line'
    _description = 'Recall Endpoint'

    recall_id = fields.Many2one('recall.recall', required=True, ondelete='cascade')
    endpoint_type = fields.Selection(ENDPOINT_SELECTION, required=True, index=True)
    proposed_action = fields.Selection(ACTION_SELECTION, required=True, default='none')
    state = fields.Selection([('todo', 'To Do'), ('done', 'Done')], default='todo', index=True)

    product_id = fields.Many2one('product.product', required=True)
    lot_id = fields.Many2one('stock.lot', string='Lot/Serial', required=True)

    # MRP link(s)
    production_id = fields.Many2one('mrp.production', string='MO')
    production_refs = fields.Char(string='MO(s)', compute='_compute_production_refs')

    # Quantities
    quantity = fields.Float(digits='Product Unit of Measure', default=0.0)
    qty_left = fields.Float(string='Qty Left', compute='_compute_qty_left', digits='Product Unit of Measure')

    # links
    location_id = fields.Many2one('stock.location')
    partner_id = fields.Many2one('res.partner')
    picking_id = fields.Many2one('stock.picking', string='Delivered Picking')
    scrap_id = fields.Many2one('stock.scrap')
    return_picking_id = fields.Many2one('stock.picking', string='Return Picking')

    company_id = fields.Many2one(related='recall_id.company_id', store=True, index=True)

    @api.depends('lot_id', 'company_id')
    def _compute_production_refs(self):
        MoveLine = self.env['stock.move.line']
        for line in self:
            if not line.lot_id:
                line.production_refs = ''
                continue
            mls = MoveLine.search([
                ('lot_id', '=', line.lot_id.id),
                ('state', '=', 'done'),
                ('company_id', '=', line.company_id.id if line.company_id else False),
                ('move_id.production_id', '!=', False),
            ])
            seen, names = set(), []
            for nm in mls.mapped('move_id.production_id.name'):
                if nm not in seen:
                    names.append(nm)
                    seen.add(nm)
            line.production_refs = ", ".join(names)

    @api.depends('state', 'quantity')
    def _compute_qty_left(self):
        for l in self:
            l.qty_left = l.quantity if l.state == 'todo' else 0.0

    @api.model
    def create(self, vals):
        rec = super().create(vals)
        rec.recall_id._auto_close_if_done()
        return rec

    def write(self, vals):
        res = super().write(vals)
        self.mapped('recall_id')._auto_close_if_done()
        return res

    def action_open_linked(self):
        self.ensure_one()
        if self.scrap_id:
            return {'type': 'ir.actions.act_window', 'res_model': 'stock.scrap', 'res_id': self.scrap_id.id, 'view_mode': 'form'}
        if self.return_picking_id:
            return {'type': 'ir.actions.act_window', 'res_model': 'stock.picking', 'res_id': self.return_picking_id.id, 'view_mode': 'form'}
        if self.picking_id:
            return {'type': 'ir.actions.act_window', 'res_model': 'stock.picking', 'res_id': self.picking_id.id, 'view_mode': 'form'}
        if self.production_id:
            return {'type': 'ir.actions.act_window', 'res_model': 'mrp.production', 'res_id': self.production_id.id, 'view_mode': 'form'}
        return True


# ---------------- Hooks ----------------
class StockScrap(models.Model):
    _inherit = 'stock.scrap'

    def action_validate(self):
        # Allow scrap reservations for recalled lots
        self = self.with_context(pv_ignore_recall_block=True)
        res = super().action_validate()
        RecallLine = self.env['recall.line']
        recalls = self.env['recall.recall']
        for scrap in self:
            qty_done = getattr(scrap, 'quantity', False) or getattr(scrap, 'scrap_qty', 0.0)
            lines = RecallLine.search([('scrap_id', '=', scrap.id)])
            for line in lines:
                planned = line.quantity or 0.0
                if qty_done + 1e-9 < planned:
                    remaining = max(planned - qty_done, 0.0)
                    if qty_done > 1e-9:
                        RecallLine.create({
                            'recall_id': line.recall_id.id,
                            'endpoint_type': line.endpoint_type,
                            'proposed_action': line.proposed_action,
                            'state': 'done',
                            'product_id': line.product_id.id,
                            'lot_id': line.lot_id.id,
                            'quantity': qty_done,
                            'location_id': line.location_id.id,
                            'scrap_id': scrap.id,
                            'production_id': line.production_id.id or False,
                        })
                    line.write({'quantity': remaining, 'scrap_id': False, 'state': 'todo'})
                    line.recall_id.message_post(
                        body=_("Partial scrap at %s → %.2f still on-hand. Generate Actions to create the next scrap.")
                        % (line.location_id.display_name, remaining)
                    )
                else:
                    if line.state != 'done':
                        line.state = 'done'
                        line.recall_id.message_post(body=_("Scrap validated → line marked Done."))
            recalls |= lines.mapped('recall_id')
        for rec in recalls:
            rec._auto_close_if_done()
        return res


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    recall_line_id = fields.Many2one('recall.line', string='Recall Line', ondelete='set null', index=True)

    # Fields used in picking form banner/buttons
    recall_acknowledged = fields.Boolean(string='Recall Acknowledged', default=False)
    recall_ids = fields.Many2many('recall.recall', string='Recalls', compute='_compute_recall_links', compute_sudo=True)
    recall_count = fields.Integer(string='Recalls', compute='_compute_recall_links', compute_sudo=True)
    recall_blocked = fields.Boolean(string='Blocked by Recall', compute='_compute_recall_blocked', compute_sudo=True)

    # --- NEW: resolve lot ids from both lot_id and lot_name on move lines
    def _recall_candidate_lot_ids(self):
        self.ensure_one()
        Lot = self.env['stock.lot'].sudo()
        lot_ids = {ml.lot_id.id for ml in self.move_line_ids if ml.lot_id}
        for ml in self.move_line_ids:
            if not ml.lot_id and ml.lot_name:
                lot = Lot.search([('name', '=', ml.lot_name), ('product_id', '=', ml.product_id.id)], limit=1)
                if lot:
                    lot_ids.add(lot.id)
        return list(lot_ids)

    def _recall_add_onhand_scrap_todo(self, line, qty_to_scrap, dest_location):
        if qty_to_scrap <= 1e-9 or not dest_location or dest_location.usage != 'internal':
            return
        RecallLine = self.env['recall.line']
        existing = RecallLine.search([
            ('recall_id', '=', line.recall_id.id),
            ('endpoint_type', '=', 'onhand'),
            ('proposed_action', '=', 'scrap'),
            ('state', '=', 'todo'),
            ('product_id', '=', line.product_id.id),
            ('lot_id', '=', line.lot_id.id),
            ('location_id', '=', dest_location.id),
            ('scrap_id', '=', False),
        ], limit=1)
        if existing:
            existing.write({'quantity': (existing.quantity or 0.0) + qty_to_scrap})
        else:
            RecallLine.create({
                'recall_id': line.recall_id.id,
                'endpoint_type': 'onhand',
                'proposed_action': 'scrap',
                'state': 'todo',
                'product_id': line.product_id.id,
                'lot_id': line.lot_id.id,
                'quantity': qty_to_scrap,
                'location_id': dest_location.id,
                'production_id': line.production_id.id or False,
            })

    def _compute_recall_links(self):
        Recall = self.env['recall.recall'].sudo()
        for picking in self:
            rec_ids = set()
            if picking.recall_line_id:
                rec_ids.add(picking.recall_line_id.recall_id.id)
            candidate_lots = picking._recall_candidate_lot_ids() if picking.id else []
            if candidate_lots:
                recs = Recall.search([
                    ('company_id', '=', picking.company_id.id),
                    ('state', 'in', ('draft', 'in_progress')),
                    ('lot_ids', 'in', candidate_lots),
                ])
                rec_ids.update(recs.ids)
            picking.recall_ids = [(6, 0, list(rec_ids))]
            picking.recall_count = len(rec_ids)

    def _compute_recall_blocked(self):
        for picking in self:
            is_relevant = bool(picking.picking_type_id and picking.picking_type_id.code in ('outgoing', 'internal'))
            if not is_relevant or picking.state in ('done', 'cancel'):
                picking.recall_blocked = False
                continue
            candidate_lots = picking._recall_candidate_lot_ids() if picking.id else []
            has_active = False
            if candidate_lots:
                has_active = bool(self.env['recall.recall'].sudo().search([
                    ('company_id', '=', picking.company_id.id),
                    ('state', 'in', ('draft', 'in_progress')),
                    ('lot_ids', 'in', candidate_lots),
                ], limit=1))
            picking.recall_blocked = bool(is_relevant and has_active and not picking.recall_acknowledged and not picking.recall_line_id)

    def action_open_recalls(self):
        self.ensure_one()
        action = self.env.ref('pv_stock_recall.action_recall_recall').read()[0]
        action['domain'] = [('id', 'in', self.recall_ids.ids)]
        return action

    def action_acknowledge_recall(self):
        if not self.env.user.has_group('pv_stock_recall.group_recall_manager'):
            raise UserError(_('Only Recall Managers may acknowledge to override recall blocks.'))
        self.write({'recall_acknowledged': True})
        for picking in self:
            picking.message_post(body=_('Recall acknowledged for this transfer by %s.') % self.env.user.display_name)
        return True

    def _p3_should_block_validation(self):
        if self.picking_type_id and self.picking_type_id.code not in ('outgoing', 'internal'):
            return False
        candidate_lots = self._recall_candidate_lot_ids()
        if not candidate_lots:
            return False
        active_recalls = self.env['recall.recall'].sudo().search([
            ('company_id', '=', self.company_id.id),
            ('state', 'in', ('draft', 'in_progress')),
            ('lot_ids', 'in', candidate_lots),
        ], limit=1)
        if not active_recalls:
            return False
        if self.recall_line_id:
            return False
        if not self.recall_acknowledged:
            return True
        return False

    def button_validate(self):
        for picking in self:
            if picking._p3_should_block_validation():
                names = ", ".join(sorted({r.name for r in self.env['recall.recall'].sudo().search([
                    ('company_id', '=', picking.company_id.id),
                    ('state', 'in', ('draft', 'in_progress')),
                    ('lot_ids', 'in', picking._recall_candidate_lot_ids()),
                ])}))
                raise UserError(_("You cannot complete this MO because it is affected by active Recall(s): %s.\n\n"
                                  "Open the Recalls from the smart button or acknowledge to continue.") % names)
        res = super().button_validate()

        # Auto split + create scrap To-Do for returned qty
        RecallLine = self.env['recall.line']
        recalls = self.env['recall.recall']
        for picking in self:
            lines = picking.recall_line_id or RecallLine.search([('return_picking_id', '=', picking.id)])
            if not lines:
                continue
            lines = lines if isinstance(lines, RecallLine.__class__) else RecallLine.browse(lines.ids)
            for line in lines:
                planned = float(line.quantity or 0.0)
                done_this = line.recall_id._qty_returned_now(
                    picking, line.picking_id, product_id=line.product_id.id, lot_id=line.lot_id.id
                )
                if done_this <= 1e-9:
                    remaining_after = line.recall_id._remaining_to_return(
                        line.picking_id, product_id=line.product_id.id, lot_id=line.lot_id.id
                    )
                    done_this = max(0.0, min(planned, planned - remaining_after))
                done_this = max(0.0, min(planned, done_this))
                remaining_after = max(0.0, planned - done_this)

                self._recall_add_onhand_scrap_todo(line, done_this, picking.location_dest_id)

                if remaining_after > 1e-9:
                    if done_this > 1e-9:
                        RecallLine.create({
                            'recall_id': line.recall_id.id,
                            'endpoint_type': line.endpoint_type,
                            'proposed_action': line.proposed_action,
                            'state': 'done',
                            'product_id': line.product_id.id,
                            'lot_id': line.lot_id.id,
                            'quantity': done_this,
                            'partner_id': line.partner_id.id if line.partner_id else False,
                            'picking_id': line.picking_id.id if line.picking_id else False,
                            'return_picking_id': picking.id,
                            'production_id': line.production_id.id or False,
                        })
                    line.write({'quantity': remaining_after, 'return_picking_id': False, 'state': 'todo'})
                    line.recall_id.message_post(
                        body=_("Partial return from %s → %.2f still to recall. Generate Actions to create the next return.")
                        % (picking.display_name, remaining_after)
                    )
                else:
                    line.write({'quantity': done_this, 'state': 'done'})
                    line.recall_id.message_post(body=_("Return validated → line marked Done."))
                recalls |= line.recall_id
        for rec in recalls:
            rec._auto_close_if_done()
        return res


class StockLot(models.Model):
    _inherit = 'stock.lot'

    recall_count = fields.Integer(string='Recalls', compute='_compute_recall_count')

    def _compute_recall_count(self):
        Recall = self.env['recall.recall'].sudo()
        for lot in self:
            domain = [('lot_ids', 'in', lot.id)]
            if lot.company_id:
                domain += [('company_id', '=', lot.company_id.id)]
            lot.recall_count = Recall.search_count(domain)

    def action_open_recalls(self):
        self.ensure_one()
        action = self.env.ref('pv_stock_recall.action_recall_recall').read()[0]
        action['domain'] = [('lot_ids', 'in', self.id)]
        action['context'] = {
            'search_default_lot_ids': self.id,
            'default_lot_ids': [(6, 0, [self.id])],
        }
        return action


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    recall_acknowledged = fields.Boolean(string='Recall Acknowledged', default=False)
    recall_ids = fields.Many2many('recall.recall', string='Recalls', compute='_compute_recall_links', compute_sudo=True)
    recall_count = fields.Integer(string='Recalls', compute='_compute_recall_links', compute_sudo=True)
    recall_blocked = fields.Boolean(string='Blocked by Recall', compute='_compute_recall_blocked', compute_sudo=True)

    def _get_param_bool(self, key, default=False):
        return self.env['ir.config_parameter'].sudo().get_param(key, default and '1' or '0') in ('1', 'true', 'True')

    def _compute_recall_links(self):
        Recall = self.env['recall.recall'].sudo()
        RLine = self.env['recall.line'].sudo()
        MoveLine = self.env['stock.move.line'].sudo()
        for mo in self:
            rec_ids = set()
            base_domain = [('state', 'in', ['draft', 'in_progress']), ('company_id', '=', mo.company_id.id)]

            rl = RLine.search([('production_id', '=', mo.id), ('recall_id.state', 'in', ['draft', 'in_progress']),
                               ('recall_id.company_id', '=', mo.company_id.id)])
            rec_ids.update(rl.mapped('recall_id').ids)

            cons_lots = MoveLine.search([
                ('move_id.raw_material_production_id', '=', mo.id),
                ('lot_id', '!=', False),
                ('state', '!=', 'cancel'),
            ]).mapped('lot_id')
            if cons_lots:
                recs = Recall.search(base_domain + [('lot_ids', 'in', cons_lots.ids)])
                rec_ids.update(recs.ids)

            if self._get_param_bool('pv_stock_recall.include_downstream', False) and cons_lots:
                rls = RLine.search([('lot_id', 'in', cons_lots.ids), ('recall_id.state', 'in', ['draft', 'in_progress']),
                                    ('recall_id.company_id', '=', mo.company_id.id)])
                rec_ids.update(rls.mapped('recall_id').ids)

            mo.recall_ids = [(6, 0, list(rec_ids))]
            mo.recall_count = len(rec_ids)

    def _compute_recall_blocked(self):
        for mo in self:
            blocked_on = self._get_param_bool('pv_stock_recall.block_wip_mos', False)
            mo.recall_blocked = bool(blocked_on and mo.recall_ids and not mo.recall_acknowledged and mo.state not in ('done', 'cancel'))

    def action_open_recalls(self):
        self.ensure_one()
        action = self.env.ref('pv_stock_recall.action_recall_recall').read()[0]
        action['domain'] = [('id', 'in', self.recall_ids.ids)]
        return action

    def action_acknowledge_recall(self):
        self.write({'recall_acknowledged': True})
        for mo in self:
            mo.message_post(body=_("Recall acknowledged for this MO by %s.") % self.env.user.display_name)
        return True

    def button_mark_done(self):
        self._compute_recall_blocked()
        blocked = self.filtered(lambda m: m.recall_blocked)
        if blocked:
            names = ", ".join(set(sum([m.recall_ids.mapped('name') for m in blocked], [])))
            raise UserError(_("You cannot complete this MO because it is affected by active Recall(s): %s.\n\n"
                              "Open the Recalls from the smart button or acknowledge to continue.") % names)

        # ensure raw-material consumption can access recalled-lot quants
        return super(MrpProduction, self.with_context(pv_ignore_recall_block=True)).button_mark_done()


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    @api.model
    def _recall_blocked_lot_ids(self, company_id):
        Recall = self.env['recall.recall'].sudo()
        recs = Recall.search([('company_id', '=', company_id), ('state', 'in', ('draft', 'in_progress'))])
        return set(recs.mapped('lot_ids').ids)

    @api.model
    def _gather(self, product_id, location_id, lot_id=None, package_id=None, owner_id=None, strict=False, **kwargs):
        """Filter out recalled-lot quants for reservations/pickings.
           Bypass for operations that set pv_ignore_recall_block=True (scrap/MO posting)."""
        quants = super()._gather(
            product_id, location_id,
            lot_id=lot_id, package_id=package_id, owner_id=owner_id, strict=strict, **kwargs
        )
        # bypass filter for operations that explicitly allow it (scrap, MO posting)
        if self.env.context.get('pv_ignore_recall_block'):
            return quants
        if not self.env['recall.recall']._p3_block_reservations():
            return quants
        blocked = self._recall_blocked_lot_ids(self.env.company.id)
        if not blocked:
            return quants
        return quants.filtered(lambda q: not q.lot_id or q.lot_id.id not in blocked)


class StockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    # --- helper to resolve a lot from incoming vals (supports lot_id and lot_name)
    def _resolve_lot_from_vals(self, vals):
        lot_id = vals.get('lot_id')
        if lot_id:
            return self.env['stock.lot'].browse(lot_id)
        lot_name = vals.get('lot_name')
        if lot_name:
            product_id = vals.get('product_id') or self.product_id.id
            return self.env['stock.lot'].search([('name', '=', lot_name), ('product_id', '=', product_id)], limit=1)
        return self.env['stock.lot']

    def _check_recall_lot_block(self, vals):
        if self.env.context.get('pv_ignore_recall_block'):
            return
        if self.env.user.has_group('pv_stock_recall.group_recall_manager'):
            return
        # Figure picking context
        picking = None
        if vals.get('picking_id'):
            picking = self.env['stock.picking'].browse(vals['picking_id'])
        elif self.picking_id:
            picking = self.picking_id
        # Skip recall-generated flows
        if picking and picking.recall_line_id:
            return
        lot = self._resolve_lot_from_vals(vals)
        if not lot:
            return
        active = self.env['recall.recall'].sudo().search([
            ('company_id', '=', (picking.company_id.id if picking else self.env.company.id)),
            ('state', 'in', ('draft', 'in_progress')),
            ('lot_ids', 'in', [lot.id]),
        ], limit=1)
        if active:
            raise UserError(_("Lot is part of an active Recall (%s) and cannot be selected here.")
                            % active.display_name)

    @api.model
    def create(self, vals):
        self._check_recall_lot_block(vals)
        return super().create(vals)

    def write(self, vals):
        self._check_recall_lot_block(vals)
        return super().write(vals)

    # reduce matching recall on-hand lines after consumption is posted
    def _recall_adjust_onhand_after_consume(self):
        RecallLine = self.env['recall.line'].sudo()
        for ml in self.filtered(lambda r:
                                r.state == 'done'
                                and r.lot_id
                                and r.location_id.usage == 'internal'
                                and r.move_id
                                and bool(r.move_id.raw_material_production_id)):
            qty = getattr(ml, 'quantity', 0.0) or getattr(ml, 'qty_done', 0.0) or 0.0
            if qty <= 1e-9:
                continue
            lines = RecallLine.search([
                ('recall_id.state', 'in', ['draft', 'in_progress']),
                ('endpoint_type', '=', 'onhand'),
                ('state', '=', 'todo'),
                ('product_id', '=', ml.product_id.id),
                ('lot_id', '=', ml.lot_id.id),
                ('location_id', '=', ml.location_id.id),
            ])
            if not lines:
                continue
            for line in lines:
                before = line.quantity or 0.0
                after = max(0.0, before - qty)
                updates = {'quantity': after}
                msg = _("%.2f consumed from %s in MO %s → on-hand scrap reduced from %.2f to %.2f.") % (
                    qty, ml.location_id.display_name, ml.move_id.raw_material_production_id.display_name, before, after
                )
                if after <= 1e-9:
                    updates['state'] = 'done'
                    msg += _(" Nothing left to scrap at this location for this lot.")
                line.write(updates)
                line.recall_id.message_post(body=msg)
                line.recall_id._auto_close_if_done()

    def _action_done(self):
        res = super()._action_done()
        self._recall_adjust_onhand_after_consume()
        return res
