#!/usr/bin/python
#
# This is a free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This Ansible library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this library.  If not, see <http://www.gnu.org/licenses/>.

DOCUMENTATION = '''
---
module: ec2_vpc_route_table
short_description: Manage route tables for AWS virtual private clouds
description:
    - Manage route tables for AWS virtual private clouds
version_added: "2.0"
author: Robert Estelle (@erydo)
options:
  vpc_id:
    description:
      - VPC ID of the VPC in which to create the route table.
    required: true
  route_table_id:
    description:
      - The ID of the route table to update or delete.
    required: false
    default: null
  resource_tags:
    description:
      - A dictionary array of resource tags of the form: { tag1: value1, tag2: value2 }. Tags in this list are used to uniquely identify route tables within a VPC when the route_table_id is not supplied.
    required: false
    default: null
  routes:
    description:
      - List of routes in the route table. Routes are specified as dicts containing the keys 'dest' and one of 'gateway_id', 'instance_id', 'interface_id', or 'vpc_peering_connection'. If 'gateway_id' is specified, you can refer to the VPC's IGW by using the value 'igw'.
    required: true
  subnets:
    description:
      - An array of subnets to add to this route table. Subnets may be specified by either subnet ID, Name tag, or by a CIDR such as '10.0.0.0/24'.
    required: true
  propagating_vgw_ids:
    description:
      - Enable route propagation from virtual gateways specified by ID.
    required: false
  wait:
    description:
      - Wait for the VPC to be in state 'available' before returning.
    required: false
    default: "no"
    choices: [ "yes", "no" ]
  wait_timeout:
    description:
      - How long before wait gives up, in seconds.
    default: 300
  state:
    description:
      - Create or destroy the VPC route table
    required: false
    default: present
    choices: [ 'present', 'absent' ]

extends_documentation_fragment: aws
'''

EXAMPLES = '''
# Note: These examples do not set authentication details, see the AWS Guide for details.

# Basic creation example:
- name: Set up public subnet route table
  local_action:
    module: ec2_vpc_route_table
    vpc_id: vpc-1245678
    region: us-west-1
    resource_tags:
      Name: Public
    subnets:
      - '{{jumpbox_subnet.subnet_id}}'
      - '{{frontend_subnet.subnet_id}}'
      - '{{vpn_subnet.subnet_id}}'
    routes:
      - dest: 0.0.0.0/0
        gateway_id: '{{igw.gateway_id}}'
  register: public_route_table

- name: Set up NAT-protected route table
  local_action:
    module: ec2_vpc_route_table
    vpc_id: vpc-1245678
    region: us-west-1
    resource_tags:
      - Name: Internal
    subnets:
      - '{{application_subnet.subnet_id}}'
      - 'Database Subnet'
      - '10.0.0.0/8'
    routes:
      - dest: 0.0.0.0/0
        instance_id: '{{nat.instance_id}}'
  register: nat_route_table
'''


import sys  # noqa
import re

try:
    import boto.ec2
    import boto.vpc
    from boto.exception import EC2ResponseError
    HAS_BOTO = True
except ImportError:
    HAS_BOTO = False
    if __name__ != '__main__':
        raise


class AnsibleRouteTableException(Exception):
    pass


class AnsibleIgwSearchException(AnsibleRouteTableException):
    pass


class AnsibleTagCreationException(AnsibleRouteTableException):
    pass


class AnsibleSubnetSearchException(AnsibleRouteTableException):
    pass

CIDR_RE = re.compile('^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$')
SUBNET_RE = re.compile('^subnet-[A-z0-9]+$')
ROUTE_TABLE_RE = re.compile('^rtb-[A-z0-9]+$')


def find_subnets(vpc_conn, vpc_id, identified_subnets):
    """
    Finds a list of subnets, each identified either by a raw ID, a unique
    'Name' tag, or a CIDR such as 10.0.0.0/8.

    Note that this function is duplicated in other ec2 modules, and should
    potentially be moved into potentially be moved into a shared module_utils
    """
    subnet_ids = []
    subnet_names = []
    subnet_cidrs = []
    for subnet in (identified_subnets or []):
        if re.match(SUBNET_RE, subnet):
            subnet_ids.append(subnet)
        elif re.match(CIDR_RE, subnet):
            subnet_cidrs.append(subnet)
        else:
            subnet_names.append(subnet)

    subnets_by_id = []
    if subnet_ids:
        subnets_by_id = vpc_conn.get_all_subnets(
            subnet_ids, filters={'vpc_id': vpc_id})

        for subnet_id in subnet_ids:
            if not any(s.id == subnet_id for s in subnets_by_id):
                raise AnsibleSubnetSearchException(
                    'Subnet ID "{0}" does not exist'.format(subnet_id))

    subnets_by_cidr = []
    if subnet_cidrs:
        subnets_by_cidr = vpc_conn.get_all_subnets(
            filters={'vpc_id': vpc_id, 'cidr': subnet_cidrs})

        for cidr in subnet_cidrs:
            if not any(s.cidr_block == cidr for s in subnets_by_cidr):
                raise AnsibleSubnetSearchException(
                    'Subnet CIDR "{0}" does not exist'.format(subnet_cidr))

    subnets_by_name = []
    if subnet_names:
        subnets_by_name = vpc_conn.get_all_subnets(
            filters={'vpc_id': vpc_id, 'tag:Name': subnet_names})

        for name in subnet_names:
            matching = [s.tags.get('Name') == name for s in subnets_by_name]
            if len(matching) == 0:
                raise AnsibleSubnetSearchException(
                    'Subnet named "{0}" does not exist'.format(name))
            elif len(matching) > 1:
                raise AnsibleSubnetSearchException(
                    'Multiple subnets named "{0}"'.format(name))

    return subnets_by_id + subnets_by_cidr + subnets_by_name


def find_igw(vpc_conn, vpc_id):
    """
    Finds the Internet gateway for the given VPC ID.

    Raises an AnsibleIgwSearchException if either no IGW can be found, or more
    than one found for the given VPC.

    Note that this function is duplicated in other ec2 modules, and should
    potentially be moved into potentially be moved into a shared module_utils
    """
    igw = vpc_conn.get_all_internet_gateways(
        filters={'attachment.vpc-id': vpc_id})

    if not igw:
        return AnsibleIgwSearchException('No IGW found for VPC "{0}"'.
                                         format(vpc_id))
    elif len(igw) == 1:
        return igw[0].id
    else:
        raise AnsibleIgwSearchException('Multiple IGWs found for VPC "{0}"'.
                                        format(vpc_id))


def get_resource_tags(vpc_conn, resource_id):
    return dict((t.name, t.value) for t in
                vpc_conn.get_all_tags(filters={'resource-id': resource_id}))


def tags_match(match_tags, candidate_tags):
    return all((k in candidate_tags and candidate_tags[k] == v
                for k, v in match_tags.iteritems()))


def ensure_tags(vpc_conn, resource_id, tags, add_only, check_mode):
    try:
        cur_tags = get_resource_tags(vpc_conn, resource_id)
        if tags == cur_tags:
            return {'changed': False, 'tags': cur_tags}

        to_delete = dict((k, cur_tags[k]) for k in cur_tags if k not in tags)
        if to_delete and not add_only:
            vpc_conn.delete_tags(resource_id, to_delete, dry_run=check_mode)

        to_add = dict((k, tags[k]) for k in tags if k not in cur_tags)
        if to_add:
            vpc_conn.create_tags(resource_id, to_add, dry_run=check_mode)

        latest_tags = get_resource_tags(vpc_conn, resource_id)
        return {'changed': True, 'tags': latest_tags}
    except EC2ResponseError as e:
        raise AnsibleTagCreationException(
            'Unable to update tags for {0}, error: {1}'.format(resource_id, e))


def get_route_table_by_id(vpc_conn, vpc_id, route_table_id):
    route_tables = vpc_conn.get_all_route_tables(
        route_table_ids=[route_table_id], filters={'vpc_id': vpc_id})
    return route_tables[0] if route_tables else None


def get_route_table_by_tags(vpc_conn, vpc_id, tags):
    route_tables = vpc_conn.get_all_route_tables(filters={'vpc_id': vpc_id})
    for route_table in route_tables:
        this_tags = get_resource_tags(vpc_conn, route_table.id)
        if tags_match(tags, this_tags):
            return route_table


def route_spec_matches_route(route_spec, route):
    key_attr_map = {
        'destination_cidr_block': 'destination_cidr_block',
        'gateway_id': 'gateway_id',
        'instance_id': 'instance_id',
        'interface_id': 'interface_id',
        'vpc_peering_connection_id': 'vpc_peering_connection_id',
    }
    for k in key_attr_map.iterkeys():
        if k in route_spec:
            if route_spec[k] != getattr(route, k):
                return False
    return True


def rename_key(d, old_key, new_key):
    d[new_key] = d[old_key]
    del d[old_key]


def index_of_matching_route(route_spec, routes_to_match):
    for i, route in enumerate(routes_to_match):
        if route_spec_matches_route(route_spec, route):
            return i


def ensure_routes(vpc_conn, route_table, route_specs, propagating_vgw_ids,
                  check_mode):
    routes_to_match = list(route_table.routes)
    route_specs_to_create = []
    for route_spec in route_specs:
        i = index_of_matching_route(route_spec, routes_to_match)
        if i is None:
            route_specs_to_create.append(route_spec)
        else:
            del routes_to_match[i]

    # NOTE: As of boto==2.38.0, the origin of a route is not available
    # (for example, whether it came from a gateway with route propagation
    # enabled). Testing for origin == 'EnableVgwRoutePropagation' is more
    # correct than checking whether the route uses a propagating VGW.
    # The current logic will leave non-propagated routes using propagating
    # VGWs in place.
    routes_to_delete = [r for r in routes_to_match
                        if r.gateway_id != 'local'
                        and r.gateway_id not in propagating_vgw_ids]

    changed = routes_to_delete or route_specs_to_create
    if changed:
        for route_spec in route_specs_to_create:
            vpc_conn.create_route(route_table.id,
                                  dry_run=check_mode,
                                  **route_spec)

        for route in routes_to_delete:
            vpc_conn.delete_route(route_table.id,
                                  route.destination_cidr_block,
                                  dry_run=check_mode)
    return {'changed': changed}


def ensure_subnet_association(vpc_conn, vpc_id, route_table_id, subnet_id,
                              check_mode):
    route_tables = vpc_conn.get_all_route_tables(
        filters={'association.subnet_id': subnet_id, 'vpc_id': vpc_id}
    )
    for route_table in route_tables:
        if route_table.id is None:
            continue
        for a in route_table.associations:
            if a.subnet_id == subnet_id:
                if route_table.id == route_table_id:
                    return {'changed': False, 'association_id': a.id}
                else:
                    if check_mode:
                        return {'changed': True}
                    vpc_conn.disassociate_route_table(a.id)

    association_id = vpc_conn.associate_route_table(route_table_id, subnet_id)
    return {'changed': True, 'association_id': association_id}


def ensure_subnet_associations(vpc_conn, vpc_id, route_table, subnets,
                               check_mode):
    current_association_ids = [a.id for a in route_table.associations]
    new_association_ids = []
    changed = False
    for subnet in subnets:
        result = ensure_subnet_association(
            vpc_conn, vpc_id, route_table.id, subnet.id, check_mode)
        changed = changed or result['changed']
        if changed and check_mode:
            return {'changed': True}
        new_association_ids.append(result['association_id'])

    to_delete = [a_id for a_id in current_association_ids
                 if a_id not in new_association_ids]

    for a_id in to_delete:
        changed = True
        vpc_conn.disassociate_route_table(a_id, dry_run=check_mode)

    return {'changed': changed}


def ensure_propagation(vpc_conn, route_table, propagating_vgw_ids,
                       check_mode):

    # NOTE: As of boto==2.38.0, it is not yet possible to query the existing
    # propagating gateways. However, EC2 does support this as shown in its API
    # documentation. For now, a reasonable proxy for this is the presence of
    # propagated routes using the gateway in the route table. If such a route
    # is found, propagation is almost certainly enabled.
    changed = False
    for vgw_id in propagating_vgw_ids:
        for r in list(route_table.routes):
            if r.gateway_id == vgw_id:
                return {'changed': False}

        changed = True
        vpc_conn.enable_vgw_route_propagation(route_table.id,
                                              vgw_id,
                                              dry_run=check_mode)

    return {'changed': changed}


def ensure_route_table_absent(vpc_conn, vpc_id, route_table_id, resource_tags,
                              check_mode):
    if route_table_id:
        route_table = get_route_table_by_id(vpc_conn, vpc_id, route_table_id)
    elif resource_tags:
        route_table = get_route_table_by_tags(vpc_conn, vpc_id, resource_tags)
    else:
        raise AnsibleRouteTableException(
            'must provide route_table_id or resource_tags')

    if route_table is None:
        return {'changed': False}

    vpc_conn.delete_route_table(route_table.id, dry_run=check_mode)
    return {'changed': True}


def ensure_route_table_present(vpc_conn, vpc_id, route_table_id, resource_tags,
                               routes, subnets, propagating_vgw_ids,
                               check_mode):
    changed = False
    tags_valid = False
    if route_table_id:
        route_table = get_route_table_by_id(vpc_conn, vpc_id, route_table_id)
    elif resource_tags:
        route_table = get_route_table_by_tags(vpc_conn, vpc_id, resource_tags)
        tags_valid = route_table is not None
    else:
        raise AnsibleRouteTableException(
            'must provide route_table_id or resource_tags')

    if check_mode and route_table is None:
        return {'changed': True}

    if route_table is None:
        try:
            route_table = vpc_conn.create_route_table(vpc_id)
        except EC2ResponseError as e:
            raise AnsibleRouteTableException(
                'Unable to create route table {0}, error: {1}'
                .format(route_table_id or resource_tags, e)
            )

    if propagating_vgw_ids is not None:
        result = ensure_propagation(vpc_conn, route_table,
                                    propagating_vgw_ids,
                                    check_mode=check_mode)
        changed = changed or result['changed']

    if not tags_valid and resource_tags is not None:
        result = ensure_tags(vpc_conn, route_table.id, resource_tags,
                             add_only=True, check_mode=check_mode)
        changed = changed or result['changed']

    if routes is not None:
        try:
            result = ensure_routes(vpc_conn, route_table, routes,
                                   propagating_vgw_ids, check_mode)
            changed = changed or result['changed']
        except EC2ResponseError as e:
            raise AnsibleRouteTableException(
                'Unable to ensure routes for route table {0}, error: {1}'
                .format(route_table, e)
            )

    if subnets:
        associated_subnets = []
        try:
            associated_subnets = find_subnets(vpc_conn, vpc_id, subnets)
        except EC2ResponseError as e:
            raise AnsibleRouteTableException(
                'Unable to find subnets for route table {0}, error: {1}'
                .format(route_table, e)
            )

        try:
            result = ensure_subnet_associations(
                vpc_conn, vpc_id, route_table, associated_subnets, check_mode)
            changed = changed or result['changed']
        except EC2ResponseError as e:
            raise AnsibleRouteTableException(
                'Unable to associate subnets for route table {0}, error: {1}'
                .format(route_table, e)
            )

    return {
        'changed': changed,
        'route_table_id': route_table.id,
    }


def main():
    argument_spec = ec2_argument_spec()
    argument_spec.update(
        dict(
            vpc_id = dict(default=None, required=True),
            route_table_id = dict(default=None, required=False),
            propagating_vgw_ids = dict(default=None, required=False, type='list'),
            resource_tags = dict(default=None, required=False, type='dict'),
            routes = dict(default=None, required=False, type='list'),
            subnets = dict(default=None, required=False, type='list'),
            state = dict(default='present', choices=['present', 'absent'])
        )
    )
    
    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)
    
    if not HAS_BOTO:
        module.fail_json(msg='boto is required for this module')

    region, ec2_url, aws_connect_params = get_aws_connection_info(module)
    
    if region:
        try:
            connection = connect_to_aws(boto.vpc, region, **aws_connect_params)
        except (boto.exception.NoAuthHandlerFound, StandardError), e:
            module.fail_json(msg=str(e))
    else:
        module.fail_json(msg="region must be specified")

    vpc_id = module.params.get('vpc_id')
    route_table_id = module.params.get('route_table_id')
    resource_tags = module.params.get('resource_tags')
    propagating_vgw_ids = module.params.get('propagating_vgw_ids', [])

    routes = module.params.get('routes')
    for route_spec in routes:
        rename_key(route_spec, 'dest', 'destination_cidr_block')

        if 'gateway_id' in route_spec and route_spec['gateway_id'] and \
                route_spec['gateway_id'].lower() == 'igw':
            igw = find_igw(connection, vpc_id)
            route_spec['gateway_id'] = igw

    subnets = module.params.get('subnets')
    state = module.params.get('state', 'present')

    try:
        if state == 'present':
            result = ensure_route_table_present(
                connection, vpc_id, route_table_id, resource_tags,
                routes, subnets, propagating_vgw_ids, module.check_mode
            )
        elif state == 'absent':
            result = ensure_route_table_absent(
                connection, vpc_id, route_table_id, resource_tags,
                module.check_mode
            )
    except AnsibleRouteTableException as e:
        module.fail_json(msg=str(e))

    module.exit_json(**result)

from ansible.module_utils.basic import *  # noqa
from ansible.module_utils.ec2 import *  # noqa

if __name__ == '__main__':
    main()
    