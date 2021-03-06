# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
# Copyright 2012 Nebula, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from django import forms
from django.utils.text import normalize_newlines
from django.utils.translation import ugettext as _

from horizon import api
from horizon import exceptions
from horizon.openstack.common import jsonutils
from horizon import workflows


class SelectProjectUserAction(workflows.Action):
    project_id = forms.ChoiceField(label=_("Project"))
    user_id = forms.ChoiceField(label=_("User"))

    def __init__(self, request, *args, **kwargs):
        super(SelectProjectUserAction, self).__init__(request, *args, **kwargs)
        # Set our project choices
        projects = [(tenant.id, tenant.name)
                    for tenant in request.user.authorized_tenants]
        self.fields['project_id'].choices = projects

        # Set our user options
        users = [(request.user.id, request.user.username)]
        self.fields['user_id'].choices = users

    class Meta:
        name = _("Project & User")
        roles = ("admin",)
        help_text = _("Admin users may optionally select the project and "
                      "user for whom the instance should be created.")


class SelectProjectUser(workflows.Step):
    action_class = SelectProjectUserAction
    contributes = ("project_id", "user_id")


class VolumeOptionsAction(workflows.Action):
    VOLUME_CHOICES = (
        ('', _("Don't boot from a volume.")),
        ("volume_id", _("Boot from volume.")),
        ("volume_snapshot_id", _("Boot from volume snapshot "
                                 "(creates a new volume).")),
    )
    # Boot from volume options
    volume_type = forms.ChoiceField(label=_("Volume Options"),
                                    choices=VOLUME_CHOICES,
                                    required=False)
    volume_id = forms.ChoiceField(label=_("Volume"), required=False)
    volume_snapshot_id = forms.ChoiceField(label=_("Volume Snapshot"),
                                           required=False)
    device_name = forms.CharField(label=_("Device Name"),
                                  required=False,
                                  initial="vda",
                                  help_text=_("Volume mount point (e.g. 'vda' "
                                              "mounts at '/dev/vda')."))
    delete_on_terminate = forms.BooleanField(label=_("Delete on Terminate"),
                                             initial=False,
                                             required=False,
                                             help_text=_("Delete volume on "
                                                         "instance terminate"))

    class Meta:
        name = _("Volume Options")
        services = ('volume',)
        help_text_template = ("nova/instances/"
                              "_launch_volumes_help.html")

    def clean(self):
        cleaned_data = super(VolumeOptionsAction, self).clean()
        volume_opt = cleaned_data.get('volume_type', None)

        if volume_opt and not cleaned_data[volume_opt]:
            raise forms.ValidationError('Please choose a volume, or select '
                                        '%s.' % self.VOLUME_CHOICES[0][1])
        return cleaned_data

    def _get_volume_display_name(self, volume):
        if hasattr(volume, "volume_id"):
            vol_type = "snap"
            visible_label = _("Snapshot")
        else:
            vol_type = "vol"
            visible_label = _("Volume")
        return (("%s:%s" % (volume.id, vol_type)),
                ("%s - %s GB (%s)" % (volume.display_name,
                                     volume.size,
                                     visible_label)))

    def populate_volume_id_choices(self, request, context):
        volume_options = [("", _("Select Volume"))]
        try:
            volumes = [v for v in api.nova.volume_list(self.request)
                       if v.status == api.VOLUME_STATE_AVAILABLE]
            volume_options.extend([self._get_volume_display_name(vol)
                                   for vol in volumes])
        except:
            exceptions.handle(self.request,
                              _('Unable to retrieve list of volumes.'))
        return volume_options

    def populate_volume_snapshot_id_choices(self, request, context):
        volume_options = [("", _("Select Volume Snapshot"))]
        try:
            snapshots = api.nova.volume_snapshot_list(self.request)
            snapshots = [s for s in snapshots
                         if s.status == api.VOLUME_STATE_AVAILABLE]
            volume_options.extend([self._get_volume_display_name(snap)
                                   for snap in snapshots])
        except:
            exceptions.handle(self.request,
                              _('Unable to retrieve list of volume '
                                'snapshots.'))

        return volume_options


class VolumeOptions(workflows.Step):
    action_class = VolumeOptionsAction
    depends_on = ("project_id", "user_id")
    contributes = ("volume_type",
                   "volume_id",
                   "device_name",  # Can be None for an image.
                   "delete_on_terminate")

    def contribute(self, data, context):
        context = super(VolumeOptions, self).contribute(data, context)
        # Translate form input to context for volume values.
        if "volume_type" in data and data["volume_type"]:
            context['volume_id'] = data.get(data['volume_type'], None)

        if not context.get("volume_type", ""):
            context['volume_type'] = self.action.VOLUME_CHOICES[0][0]
            context['volume_id'] = None
            context['device_name'] = None
            context['delete_on_terminate'] = None
        return context


class SetInstanceDetailsAction(workflows.Action):
    SOURCE_TYPE_CHOICES = (
        ("image_id", _("Image")),
        ("instance_snapshot_id", _("Snapshot")),
    )
    source_type = forms.ChoiceField(label=_("Instance Source"),
                                    choices=SOURCE_TYPE_CHOICES)
    image_id = forms.ChoiceField(label=_("Image"), required=False)
    instance_snapshot_id = forms.ChoiceField(label=_("Instance Snapshot"),
                                             required=False)
    name = forms.CharField(max_length=80, label=_("Server Name"))
    flavor = forms.ChoiceField(label=_("Flavor"),
                               help_text=_("Size of image to launch."))
    count = forms.IntegerField(label=_("Instance Count"),
                               min_value=1,
                               initial=1,
                               help_text=_("Number of instances to launch."))

    class Meta:
        name = _("Details")
        help_text_template = ("nova/instances/"
                              "_launch_details_help.html")

    def clean(self):
        cleaned_data = super(SetInstanceDetailsAction, self).clean()

        # Validate our instance source.
        source = cleaned_data['source_type']
        if not cleaned_data[source]:
            raise forms.ValidationError("Please select an option for the "
                                        "instance source.")

        # Prevent launching multiple instances with the same volume.
        # TODO(gabriel): is it safe to launch multiple instances with
        # a snapshot since it should be cloned to new volumes?
        count = cleaned_data.get('count', 1)
        volume_type = self.data.get('volume_type', None)
        if volume_type and count > 1:
            msg = _('Launching multiple instances is only supported for '
                    'images and instance snapshots.')
            raise forms.ValidationError(msg)

        return cleaned_data

    def _get_available_images(self, request, context):
        project_id = context.get('project_id', None)
        if not hasattr(self, "_public_images"):
            public = {"is_public": True}
            try:
                public_images, _more = api.glance.image_list_detailed(request,
                                                           filters=public)
            except:
                public_images = []
                exceptions.handle(request,
                                  _("Unable to retrieve public images."))
            self._public_images = public_images

        # Preempt if we don't have a project_id yet.
        if project_id is None:
            setattr(self, "_images_for_%s" % project_id, [])

        if not hasattr(self, "_images_for_%s" % project_id):
            owner = {"property-owner_id": project_id}
            try:
                owned_images, _more = api.glance.image_list_detailed(request,
                                                          filters=owner)
            except:
                exceptions.handle(request,
                                  _("Unable to retrieve images for "
                                    "the current project."))
            setattr(self, "_images_for_%s" % project_id, owned_images)

        owned_images = getattr(self, "_images_for_%s" % project_id)
        images = owned_images + self._public_images

        # Remove duplicate images.
        image_ids = []
        final_images = []
        for image in images:
            if image.id not in image_ids:
                image_ids.append(image.id)
                final_images.append(image)
        return [image for image in final_images
                if image.container_format not in ('aki', 'ari')]

    def populate_image_id_choices(self, request, context):
        images = self._get_available_images(request, context)
        choices = [(image.id, image.name)
                   for image in images
                   if image.properties.get("image_type", '') != "snapshot"]
        if choices:
            choices.insert(0, ("", _("Select Image")))
        else:
            choices.insert(0, ("", _("No images available.")))
        return choices

    def populate_instance_snapshot_id_choices(self, request, context):
        images = self._get_available_images(request, context)
        choices = [(image.id, image.name)
                   for image in images
                   if image.properties.get("image_type", '') == "snapshot"]
        if choices:
            choices.insert(0, ("", _("Select Instance Snapshot")))
        else:
            choices.insert(0, ("", _("No snapshots available.")))
        return choices

    def populate_flavor_choices(self, request, context):
        try:
            flavors = api.nova.flavor_list(request)
            flavor_list = [(flavor.id, "%s" % flavor.name)
                           for flavor in flavors]
        except:
            flavor_list = []
            exceptions.handle(request,
                              _('Unable to retrieve instance flavors.'))
        return sorted(flavor_list)

    def get_help_text(self):
        extra = {}
        try:
            extra['usages'] = api.nova.tenant_quota_usages(self.request)
            extra['usages_json'] = jsonutils.dumps(extra['usages'])
            flavors = jsonutils.dumps([f._info for f in
                                       api.nova.flavor_list(self.request)])
            extra['flavors'] = flavors
        except:
            exceptions.handle(self.request,
                              _("Unable to retrieve quota information."))
        return super(SetInstanceDetailsAction, self).get_help_text(extra)


class SetInstanceDetails(workflows.Step):
    action_class = SetInstanceDetailsAction
    contributes = ("source_type", "source_id", "name", "count", "flavor")

    def prepare_action_context(self, request, context):
        if 'source_type' in context and 'source_id' in context:
            context[context['source_type']] = context['source_id']
        return context

    def contribute(self, data, context):
        context = super(SetInstanceDetails, self).contribute(data, context)
        # Allow setting the source dynamically.
        if ("source_type" in context and "source_id" in context
                and context["source_type"] not in context):
            context[context["source_type"]] = context["source_id"]

        # Translate form input to context for source values.
        if "source_type" in data:
            context["source_id"] = data.get(data['source_type'], None)

        return context


class SetAccessControlsAction(workflows.Action):
    keypair = forms.ChoiceField(label=_("Keypair"),
                                required=False,
                                help_text=_("Which keypair to use for "
                                            "authentication."))
    groups = forms.MultipleChoiceField(label=_("Security Groups"),
                                       required=True,
                                       initial=["default"],
                                       widget=forms.CheckboxSelectMultiple(),
                                       help_text=_("Launch instance in these "
                                                   "security groups."))

    class Meta:
        name = _("Access & Security")
        help_text = _("Control access to your instance via keypairs, "
                      "security groups, and other mechanisms.")

    def populate_keypair_choices(self, request, context):
        try:
            keypairs = api.nova.keypair_list(request)
            keypair_list = [(kp.name, kp.name) for kp in keypairs]
        except:
            keypair_list = []
            exceptions.handle(request,
                              _('Unable to retrieve keypairs.'))
        if keypair_list:
            keypair_list.insert(0, ("", _("Select a keypair")))
        else:
            keypair_list = (("", _("No keypairs available.")),)
        return keypair_list

    def populate_groups_choices(self, request, context):
        try:
            groups = api.nova.security_group_list(request)
            security_group_list = [(sg.name, sg.name) for sg in groups]
        except:
            exceptions.handle(request,
                              _('Unable to retrieve list of security groups'))
            security_group_list = []
        return security_group_list


class SetAccessControls(workflows.Step):
    action_class = SetAccessControlsAction
    depends_on = ("project_id", "user_id")
    contributes = ("keypair_id", "security_group_ids")

    def contribute(self, data, context):
        if data:
            post = self.workflow.request.POST
            context['security_group_ids'] = post.getlist("groups")
            context['keypair_id'] = data.get("keypair", "")
        return context


class CustomizeAction(workflows.Action):
    customization_script = forms.CharField(widget=forms.Textarea,
                                           label=_("Customization Script"),
                                           required=False,
                                           help_text=_("A script or set of "
                                                       "commands to be "
                                                       "executed after the "
                                                       "instance has been "
                                                       "built (max 16kb)."))

    class Meta:
        name = _("Post-Creation")
        help_text_template = ("nova/instances/"
                              "_launch_customize_help.html")


class PostCreationStep(workflows.Step):
    action_class = CustomizeAction
    contributes = ("customization_script",)


class LaunchInstance(workflows.Workflow):
    slug = "launch_instance"
    name = _("Launch Instance")
    finalize_button_name = _("Launch")
    success_message = _('Launched %s named "%s".')
    failure_message = _('Unable to launch %s named "%s".')
    success_url = "horizon:nova:instances:index"
    default_steps = (SelectProjectUser,
                     SetInstanceDetails,
                     SetAccessControls,
                     VolumeOptions,
                     PostCreationStep)

    def format_status_message(self, message):
        name = self.context.get('name', 'unknown instance')
        count = self.context.get('count', 1)
        if int(count) > 1:
            return message % (_("%s instances") % count, name)
        else:
            return message % (_("instance"), name)

    def handle(self, request, context):
        custom_script = context.get('customization_script', '')

        # Determine volume mapping options
        if context.get('volume_type', None):
            if(context['delete_on_terminate']):
                del_on_terminate = 1
            else:
                del_on_terminate = 0
            mapping_opts = ("%s::%s"
                            % (context['volume_id'], del_on_terminate))
            dev_mapping = {context['device_name']: mapping_opts}
        else:
            dev_mapping = None

        try:
            api.nova.server_create(request,
                                   context['name'],
                                   context['source_id'],
                                   context['flavor'],
                                   context['keypair_id'],
                                   normalize_newlines(custom_script),
                                   context['security_group_ids'],
                                   dev_mapping,
                                   instance_count=int(context['count']))
            return True
        except:
            exceptions.handle(request)
            return False
