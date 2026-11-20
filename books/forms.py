"""基础数据维护用的 ModelForm（Bootstrap 样式）。"""
from django import forms
from books.models import (
    NameMapping, CustomerSalesperson, Operator, LogicRuleType, MappingStep, LogicRule,
)


class NameMappingForm(forms.ModelForm):
    class Meta:
        model = NameMapping
        fields = ['raw_name', 'canonical_name', 'tax_no']
        widgets = {
            'raw_name': forms.TextInput(attrs={'class': 'form-control'}),
            'canonical_name': forms.TextInput(attrs={'class': 'form-control'}),
            'tax_no': forms.TextInput(attrs={'class': 'form-control'}),
        }


class CustomerSalespersonForm(forms.ModelForm):
    class Meta:
        model = CustomerSalesperson
        fields = ['unit_name', 'salesperson', 'is_current', 'source', 'effective_date', 'credit_term']
        widgets = {
            'unit_name': forms.TextInput(attrs={'class': 'form-control'}),
            'salesperson': forms.TextInput(attrs={'class': 'form-control'}),
            'is_current': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'source': forms.Select(attrs={'class': 'form-select'}),
            'effective_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'credit_term': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
        }


class CustomerSalespersonCreditTermForm(forms.ModelForm):
    """业务员专用：仅可编辑自己客户的「账期天数」，其余字段不可改。"""
    class Meta:
        model = CustomerSalesperson
        fields = ['credit_term']
        widgets = {
            'credit_term': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
        }


def _build_dynamic_fields(form, schema_source):
    """按 params_schema 注入动态字段（无 JS）；编辑时从 instance.params 取初值。"""
    schema = schema_source or []
    data_params = {}
    inst = getattr(form, 'instance', None)
    if inst and isinstance(getattr(inst, 'params', None), dict):
        data_params = inst.params
    for p in schema:
        name = p['name']
        label = p.get('label', name)
        ftype = p.get('type', 'str')
        required = bool(p.get('required', False))
        default = p.get('default', '')
        choices = p.get('choices')
        if ftype == 'int':
            field = forms.IntegerField(label=label, required=required,
                                       initial=data_params.get(name, default))
        elif ftype == 'bool':
            field = forms.BooleanField(label=label, required=False,
                                       initial=bool(data_params.get(name, default)))
        elif choices:
            field = forms.ChoiceField(label=label, required=required,
                                      choices=[('', '—')] + [(c, c) for c in choices],
                                      initial=data_params.get(name, default))
        else:
            field = forms.CharField(label=label, required=required,
                                    initial=data_params.get(name, default))
        field.widget.attrs.update({'class': 'form-control form-control-sm'})
        form.fields[name] = field


def _aggregate_params(cleaned_data, schema_source):
    """把动态字段的值汇总成 params dict。"""
    params = {}
    schema = schema_source or []
    op = cleaned_data.get('op') or cleaned_data.get('rule_type')
    if not op:
        return params
    schema = getattr(op, 'params_schema', None) or []
    for p in schema:
        name = p['name']
        if name in cleaned_data:
            params[name] = cleaned_data[name]
    return params


class MappingStepForm(forms.ModelForm):
    """映射步骤：选算子 + 按该算子参数 schema 动态填参。params 存为隐藏字段。"""
    class Meta:
        model = MappingStep
        fields = ['op', 'order', 'params']
        widgets = {'params': forms.HiddenInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        op = None
        if self.is_bound:
            op_id = self.data.get(self.add_prefix('op'))
            if op_id:
                op = Operator.objects.filter(pk=op_id).first()
        elif self.instance.pk and getattr(self.instance, 'op_id', None):
            op = self.instance.op
        if op:
            _build_dynamic_fields(self, op.params_schema)

    def clean(self):
        cleaned = super().clean()
        cleaned['params'] = _aggregate_params(cleaned, None)
        return cleaned


class LogicRuleForm(forms.ModelForm):
    """逻辑规则：选规则块 + 按规则块参数 schema 动态填参。params 存为隐藏字段。"""
    class Meta:
        model = LogicRule
        fields = ['source_type', 'rule_type', 'order', 'is_active', 'params']
        widgets = {'params': forms.HiddenInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        rt = None
        if self.is_bound:
            rt_id = self.data.get(self.add_prefix('rule_type'))
            if rt_id:
                rt = LogicRuleType.objects.filter(pk=rt_id).first()
        elif self.instance.pk and getattr(self.instance, 'rule_type_id', None):
            rt = self.instance.rule_type
        if rt:
            _build_dynamic_fields(self, rt.params_schema)

    def clean(self):
        cleaned = super().clean()
        cleaned['params'] = _aggregate_params(cleaned, None)
        return cleaned
