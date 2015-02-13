#!/usr/bin/python
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

# This is a DOCUMENTATION stub specific to this module, it extends
# a documentation fragment located in ansible.utils.module_docs_fragments
DOCUMENTATION = '''
---
module: rax_mon_alarm
short_description: Create or delete a Rackspace Cloud Monitoring alarm.
description:
- Create or delete a Rackspace Cloud Monitoring alarm that associates an
  existing rax_mon_entity, rax_mon_check, and rax_mon_notification_plan with
  criteria that specify what conditions will trigger which levels of
  notifications. Rackspace monitoring module flow | rax_mon_entity ->
  rax_mon_check -> rax_mon_notification -> rax_mon_notification_plan ->
  *rax_mon_alarm*
version_added: "1.8.2"
options:
  state:
    description:
    - Ensure that the alarm with this C(label) exists or does not exist.
    choices: [ "present", "absent" ]
    required: false
    default: present
  label:
    description:
    - Friendly name for this alarm, used to achieve idempotence. Must be a String
      between 1 and 255 characters long.
    required: true
  entity_id:
    description:
    - ID of the entity this alarm is attached to. May be acquired by registering
      the value of a rax_mon_entity task.
    required: true
  check_id:
    description:
    - ID of the check that should be alerted on. May be acquired by registering
      the value of a rax_mon_check task.
    required: true
  notification_plan_id:
    description:
    - ID of the notification plan to trigger if this alarm fires. May be acquired
      by registering the value of a rax_mon_notification_plan task.
    required: true
  criteria:
    description:
    - Alarm DSL that describes alerting conditions and their output states. Must
      be between 1 and 16384 characters long. See
      http://docs.rackspace.com/cm/api/v1.0/cm-devguide/content/alerts-language.html
      for a reference on the alerting language.
  disabled:
    description:
    - If yes, create this alarm, but leave it in an inactive state. Defaults to
      no.
    choices: [ "yes", "no" ]
  metadata:
    description:
    - Arbitrary key/value pairs to accompany the alarm. Must be a hash of String
      keys and values between 1 and 255 characters long.
author: Ash Wilson
extends_documentation_fragment: rackspace.openstack
'''

EXAMPLES = '''
- name: Alarm example
  gather_facts: False
  hosts: local
  connection: local
  tasks:
  - name: Ensure that a specific alarm exists.
    rax_mon_alarm:
      credentials: ~/.rax_pub
      state: present
      label: uhoh
      entity_id: "{{ the_entity['entity']['id'] }}"
      check_id: "{{ the_check['check']['id'] }}"
      notification_plan_id: "{{ defcon1['notification_plan']['id'] }}"
      criteria: >
        if (rate(metric['average']) > 10) {
          return new AlarmStatus(WARNING);
        }
        return new AlarmStatus(OK);
    register: the_alarm
'''

try:
    import pyrax
    HAS_PYRAX = True
except ImportError:
    HAS_PYRAX = False

def alarm(module, state, label, entity_id, check_id, notification_plan_id, criteria,
          disabled, metadata):

    # Verify the presence of required attributes.

    required_attrs = {
        "label": label, "entity_id": entity_id, "check_id": check_id,
        "notification_plan_id": notification_plan_id
    }

    for (key, value) in required_attrs.iteritems():
        if not value:
            module.fail_json(msg=('%s is required for rax_mon_alarm' % key))

    if len(label) < 1 or len(label) > 255:
        module.fail_json(msg='label must be between 1 and 255 characters long')

    if criteria and len(criteria) < 1 or len(criteria) > 16384:
        module.fail_json(msg='criteria must be between 1 and 16384 characters long')

    # Coerce attributes.

    changed = False
    alarm = None

    cm = pyrax.cloud_monitoring
    if not cm:
        module.fail_json(msg='Failed to instantiate client. This typically '
                             'indicates an invalid region or an incorrectly '
                             'capitalized region name.')

    existing = [a for a in cm.list_alarms(entity_id) if a.label == label]

    if existing:
        alarm = existing[0]

    if state == 'present':
        should_create = False
        should_update = False
        should_delete = False

        if len(existing) > 1:
            module.fail_json(msg='%s existing alarms have the label %s.' %
                                 (len(existing), label))

        if alarm:
            if check_id != alarm.check_id or notification_plan_id != alarm.notification_plan_id:
                should_delete = should_create = True

            should_update = (disabled and disabled != alarm.disabled) or \
                (metadata and metadata != alarm.metadata) or \
                (criteria and criteria != alarm.criteria)

            if should_update and not should_delete:
                cm.update_alarm(entity=entity_id, alarm=alarm,
                                criteria=criteria, disabled=disabled,
                                label=label, metadata=metadata)
                changed = True

            if should_delete:
                alarm.delete()
                changed = True
        else:
            should_create = True

        if should_create:
            alarm = cm.create_alarm(entity=entity_id, check=check_id,
                                    notification_plan=notification_plan_id,
                                    criteria=criteria, disabled=disabled, label=label,
                                    metadata=metadata)
            changed = True
    elif state == 'absent':
        for a in existing:
            a.delete()
            changed = True
    else:
        module.fail_json(msg='state must be either present or absent.')

    if alarm:
        alarm_dict = {
            "id": alarm.id,
            "label": alarm.label,
            "check_id": alarm.check_id,
            "notification_plan_id": alarm.notification_plan_id,
            "criteria": alarm.criteria,
            "disabled": alarm.disabled,
            "metadata": alarm.metadata
        }
        module.exit_json(changed=changed, alarm=alarm_dict)
    else:
        module.exit_json(changed=changed)

def main():
    argument_spec = rax_argument_spec()
    argument_spec.update(
        dict(
            state=dict(default='present'),
            label=dict(),
            entity_id=dict(),
            check_id=dict(),
            notification_plan_id=dict(),
            criteria=dict(),
            disabled=dict(type='bool', default=False),
            metadata=dict(type='dict')
        )
    )

    module = AnsibleModule(
        argument_spec=argument_spec,
        required_together=rax_required_together()
    )

    if not HAS_PYRAX:
        module.fail_json(msg='pyrax is required for this module')

    state = module.params.get('state')
    label = module.params.get('label')
    entity_id = module.params.get('entity_id')
    check_id = module.params.get('check_id')
    notification_plan_id = module.params.get('notification_plan_id')
    criteria = module.params.get('criteria')
    disabled = module.boolean(module.params.get('disabled'))
    metadata = module.params.get('metadata')

    setup_rax_module(module, pyrax)

    alarm(module, state, label, entity_id, check_id, notification_plan_id,
          criteria, disabled, metadata)


# Import module snippets
from ansible.module_utils.basic import *
from ansible.module_utils.rax import *

# Invoke the module.
main()