"""
Params.py

"""

__author__ = 'Halley Nathwani'
__copyright__ = 'Copyright 2018. Electric Power Research Institute (EPRI). All Rights Reserved.'
__credits__ = ['Miles Evans', 'Andres Cortes', 'Evan Giarta', 'Halley Nathwani',  "Thien Nguyen"]
__license__ = 'EPRI'
__maintainer__ = ['Halley Nathwani', 'Miles Evans']
__email__ = ['hnathwani@epri.com', 'mevans@epri.com']
__version__ = 'beta'  # beta version

import xml.etree.ElementTree as et
import pandas as pd
import numpy as np
from storagevet.Params import Params
import copy
from ErrorHandelling import *
from pathlib import Path


class ParamsDER(Params):
    """
        Class attributes are made up of services, technology, and any other needed inputs. The attributes are filled
        by converting the xml file in a python object.

        Notes:
             Need to change the summary functions for pre-visualization every time the Params class is changed - TN
    """
    # set schema location based on the location of this file (this should override the global value within Params.py
    schema_location = Path(__file__).absolute().with_name('DERVETSchema.xml')
    cba_input_error_raised = False
    cba_input_template = None

    @staticmethod
    def csv_to_xml(csv_filename, verbose=False, ignore_cba_valuation=False):
        """ converts csv to 2 xml files. One that contains values that correspond to optimization values and the other
        corresponds the values used to evaluate the CBA.

        Args:
            csv_filename (string): name of csv file
            ignore_cba_valuation (bool): flag to tell whether to look at the evaluation columns provided (meant for
                testing purposes)
            verbose (bool): whether or not to print to console for more feedback


        Returns:
            opt_xml_filename (string): name of xml file with parameter values for optimization evaluation


        """
        xml_filename = Params.csv_to_xml(csv_filename, verbose)

        # open csv to read into dataframe and blank xml file to write to
        csv_data = pd.read_csv(csv_filename)
        # check to see if Evaluation rows are included
        if not ignore_cba_valuation and 'Evaluation Value' in csv_data.columns and 'Evaluation Active' in csv_data.columns:
            # then add values to XML

            # open and read xml file
            xml_tree = et.parse(xml_filename)
            xml_root = xml_tree.getroot()

            # outer loop for each tag/object and active status, i.e. Scenario, Battery, DA, etc.
            for obj in csv_data.Tag.unique():
                mask = csv_data.Tag == obj
                tag = xml_root.find(obj)
                # middle loop for each object's elements and is sensitivity is needed: max_ch_rated, ene_rated, price, etc.
                for ind, row in csv_data[mask].iterrows():
                    # skip adding to XML if no value is given
                    if row['Key'] is np.nan or row['Evaluation Value'] == '.' or row['Evaluation Active'] == '.':
                        continue
                    key = tag.find(row['Key'])
                    cba_eval = et.SubElement(key, 'Evaluation')
                    cba_eval.text = str(row['Evaluation Value'])
                    cba_eval.set('active', str(row['Evaluation Active']))
            xml_tree.write(xml_filename)

        return xml_filename

    @classmethod
    def initialize(cls, filename, verbose):
        """ In addition to everything that initialize does in Params, this class will look at
        Evaluation Value to - 1) determine if cba value can be given and validate; 2) convert
        any referenced data into direct data 3) if sensitivity analysis, then make sure enough
        cba values are given 4) build a dictionary of CBA inputs that match with the instances
        that need to be run

            Args:
                filename (string): filename of XML or CSV model parameter
                verbose (bool): whether or not to print to console for more feedback

            Returns dictionary of instances of Params, each key is a number
        """
        cls.instances = super().initialize(filename, verbose)  # everything that initialize does in Params (steps 1-4)
        # 1) INITIALIZE CLASS VARIABLES
        cls.sensitivity['cba_values'] = dict()
        cls.cba_input_error_raised = False

        # 5) load direct data and create input template
        # determine if cba value can be given and validate
        cls.cba_input_template = cls.cba_template_struct()

        # convert any referenced data into direct data (add referenced data to dict DATASETS)
        cls.read_evaluation_referenced_data()

        # report back any warning associated with the 'Evaulation' column
        if cls.cba_input_error_raised:
            raise AssertionError("The model parameter has some errors associated to it in the CBA column. Please fix and rerun.")

        # 6) if SA, update case definitions to define which CBA values will apply for each case
        cls.add_evaluation_to_case_definitions()

        # 7) build a dictionary of CBA inputs that matches with the instance Params that the inputs should be paired with and
        # load up datasets that correspond with referenced data in respective cba_input_instance (as defined by CASE_DEFINITIONS)
        # distribute CBA dictionary of inputs to the corresponding Param instance (so its value can be passed on to Scenario)
        cls.cba_input_builder()

        return cls.instances

    def __init__(self):
        """ Initialize these following attributes of the empty Params class object.
        """
        super().__init__()
        self.Reliability = self.read_and_validate('Reliability')  # Value Stream
        self.Load = self.read_and_validate('ControllableLoad')  # DER

    @classmethod
    def bad_active_combo(cls):
        """ Based on what the user has indicated as active (and what the user has not), predict whether or not
        the simulation(s) will have trouble solving.

        Returns (bool): True if there is no errors found. False if there is errors found in the errors log.

        """
        is_dervet_specific_tech_active = not len
        super().bad_active_combo(dervet=True)

    @classmethod
    def cba_template_struct(cls):
        """

        Returns: a template structure that summarizes the inputs for a CBA instance

        """
        template = dict()
        template['Scenario'] = cls.read_and_validate_evaluation('Scenario')
        template['Finance'] = cls.read_and_validate_evaluation('Finance')

        # create dictionary for CBA values for DERs
        template['ders_values'] = {
            'Battery': cls.read_and_validate_evaluation('Battery'),
            'CAES': cls.read_and_validate_evaluation('CAES'),
            'PV': cls.read_and_validate_evaluation('PV'),  # cost_per_kW (and then recalculate capex)
            'ICE': cls.read_and_validate_evaluation('ICE'),  # fuel_price,
            # 'ControllableLoad': cls.read_and_validate_evaluation('ControllableLoad')
        }

        # create dictionary for CBA values for all services (from data files)
        template['valuestream_values'] = {'User': cls.read_and_validate_evaluation('User'),  # only have one entry in it (key = price)
                                          'Deferral': cls.read_and_validate_evaluation('Deferral')}
        return template

    @classmethod
    def read_and_validate_evaluation(cls, name):
        """ Read data from valuation XML file

        Args:
            name (str): name of root element in xml file

        Returns: A dictionary where keys are the ID value and the key is a dictionary
            filled with values provided by user that will be used by the CBA class
            or None if no values are active.

        """
        schema_tag = cls.schema_tree.find(name)
        # Check if tag is in schema (SANITY CHECK)
        if schema_tag is None:
            cls.report_warning("missing tag", tag=name, raise_input_error=False)
            # warn user that the tag given is not in the schema
            return
        tag_elems = cls.xmlTree.findall(name)
        # check to see if user includes the tag within the provided xml
        if tag_elems is None:
            return
        tag_data_struct = {}
        for tag in tag_elems:
            # This statement checks if the first character is 'y' or '1', if true it creates a dictionary.
            if tag.get('active')[0].lower() == "y" or tag.get('active')[0] == "1":
                dictionary = {}
                # iterate through each key required by the schema
                for schema_key in schema_tag:
                    key = tag.find(schema_key.tag)
                    cba_value = key.find('Evaluation')
                    # if we dont have a cba_value, skip to next key
                    if cba_value is None:
                        continue
                    # did the user mark cba input as active?
                    if cba_value.get('active')[0].lower() == "y" or cba_value.get('active')[0] == "1":
                        # check if you are allowed to input Evaulation value for the give key
                        if schema_key.get('cba')[0].lower() in ['y', '1']:
                            valuation_entry = None
                            intended_type = key.find('Type').text
                            if key.get('analysis')[0].lower() == 'y' or key.get('analysis')[0].lower() == '1':
                                # if analysis, then convert each value and save as list
                                tag_key = (tag.tag, key.tag, tag.get('id'))
                                sensitivity_values = cls.extract_data(key.find('Evaluation').text, intended_type)

                                # validate each value
                                for values in sensitivity_values:
                                    cls.checks_for_validate(values, schema_key, name)

                                #  check to make sure the length match with sensitivity analysis value set length
                                required_values = len(cls.sensitivity['attributes'][tag_key])
                                if required_values != len(sensitivity_values):
                                    cls.report_warning('cba sa length', tag=name, key=key.tag, required_num=required_values)
                                cls.sensitivity['cba_values'][tag_key] = sensitivity_values
                            else:
                                # convert to correct data type
                                valuation_entry = cls.convert_data_type(key.find('Evaluation').text, intended_type)
                                cls.checks_for_validate(valuation_entry, schema_key, f"{name}-{tag.get('id')}")
                            # save evaluation value OR save a place for the sensitivity value to fill in the dictionary later w/ None
                            dictionary[key.tag] = valuation_entry
                        else:
                            cls.report_warning('cba not allowed', tag=name, key=key.tag, raise_input_error=False)
                # save set of KEYS (in the dictionary) to the TAG that it belongs to (multiple dictionaries if mutliple IDs)
                tag_data_struct[tag.get('id')] = dictionary
        return tag_data_struct

    @classmethod
    def report_warning(cls, warning_type, raise_input_error=True, **kwargs):
        """ Print a warning to the user log. Warnings are reported, but do not result in exiting.

        Args:
            warning_type (str): the classification of the warning to be reported to the user
            raise_input_error (bool): raise this warning as an error instead back to the user and stop running
                the program
            kwargs: elements about the warning that need to be reported to the user (like the tag and key that
                caused the error

        """
        if warning_type == "too many tags":
            TellUser.error(f"INPUT: There are {kwargs['length']} {kwargs['tag']}'s, but only {kwargs['max']} can be defined")

        if warning_type == 'cba not allowed':
            TellUser.error(f"INPUT: {kwargs['tag']}-{kwargs['key']} is not be used within the " +
                           "CBA module of the program. Value is ignored.")
            cls.cba_input_error_raised = raise_input_error or cls.cba_input_error_raised
        if warning_type == "cba sa length":
            cls.cba_input_error_raised = raise_input_error or cls.cba_input_error_raised
            TellUser.error(f"INPUT: {kwargs['tag']}-{kwargs['key']} has not enough CBA evaluatino values to "
                           f"successfully complete sensitivity analysis. Please include {kwargs['required_num']} "
                           f"values, each corresponding to the Sensitivity Analysis value given")
        super().report_warning(warning_type, raise_input_error, **kwargs)

    @classmethod
    def read_evaluation_referenced_data(cls):
        """ This function makes a unique set of filename(s) based on grab_evaluation_lst and the data already read into REFERENCED_DATA.
            It applies for time series filename(s), monthly data filename(s), customer tariff filename(s).
            For each set, the corresponding class dataset variable (ts, md, ct) is loaded with the data.

            Preprocess monthly data files

        """

        ts_files = cls.grab_evaluation_lst('Scenario', 'time_series_filename') - set(cls.referenced_data['time_series'].keys())
        md_files = cls.grab_evaluation_lst('Scenario', 'monthly_data_filename') - set(cls.referenced_data['monthly_data'].keys())
        ct_files = cls.grab_evaluation_lst('Finance', 'customer_tariff_filename') - set(cls.referenced_data['customer_tariff'].keys())
        yr_files = cls.grab_evaluation_lst('Finance', 'yearly_data_filename') - set(cls.referenced_data['yearly_data'].keys())

        for ts_file in ts_files:
            cls.referenced_data['time_series'][ts_file] = cls.read_from_file('time_series', ts_file, 'Datetime (he)')
        for md_file in md_files:
            cls.referenced_data['monthly_data'][md_file] = cls.preprocess_monthly(cls.read_from_file('monthly_data', md_file, ['Year', 'Month']))
        for ct_file in ct_files:
            cls.referenced_data['customer_tariff'][ct_file] = cls.read_from_file('customer_tariff', ct_file, 'Billing Period')
        for yr_file in yr_files:
            cls.referenced_data['yearly_data'][yr_file] = cls.read_from_file('yearly_data', yr_file, 'Year')

    @classmethod
    def grab_evaluation_lst(cls, tag, key):
        """ Checks if the tag-key exists in cls.sensitivity, otherwise grabs the base case value
        from cls.template

        Args:
            tag (str):
            key (str):

        Returns: set of values

        """
        values = []

        tag_dict = cls.cba_input_template.get(tag)
        if tag_dict is not None:
            for id_str in tag_dict.keys():
                try:
                    values += list(cls.sensitivity['cba_values'][(tag, key, id_str)])
                except KeyError:
                    try:
                        values += [cls.cba_input_template[tag][id_str][key]]
                    except KeyError:
                        pass
        return set(values)

    @classmethod
    def add_evaluation_to_case_definitions(cls):
        """ Method that adds the 'Evaluation' values as a column to the dataframe that defines the differences in the cases
        being run.

        """
        cba_sensi = cls.sensitivity['cba_values']
        # for each tag-key cba value that sensitivity analysis applies to
        for tag_key, value_lst in cba_sensi.items():
            # initialize the new column with 'NaN'
            cls.case_definitions[f"CBA {tag_key}"] = np.NAN
            # get the number of values that you will need to iterate through
            num_cba_values = len(value_lst)
            # for each index in VALUE_LST
            for index in range(num_cba_values):
                corresponding_opt_value = cls.sensitivity['attributes'][tag_key][index]
                # find the row(s) that contain the optimization value that was also the INDEX-th value in the Sensitivity Parameters entry
                cls.case_definitions.loc[cls.case_definitions[tag_key] == corresponding_opt_value, f"CBA {tag_key}"] = value_lst[index]

        # check for any entries w/ NaN to make sure everything went fine
        if np.any(cls.case_definitions == np.NAN):
            TellUser.debug('There are some left over Nans in the case definition. Something went wrong.')

    @classmethod
    def cba_input_builder(cls):
        """
            Function to create all the possible combinations of inputs to correspond to the sensitivity analysis case being run

        """
        # while case definitions is not an empty df (there is SA) or if it is the last row in case definitions
        for index in cls.instances.keys():
            cba_dict = copy.deepcopy(cls.cba_input_template)
            # check to see if there are any CBA values included in case definition OTHERWISE just read in any referenced data
            for tag_key_id in cls.sensitivity['cba_values'].keys():
                row = cls.case_definitions.iloc[index]
                # modify the case dictionary
                if tag_key_id[0] in cls.cba_input_template['ders_values'].keys():
                    cba_dict['ders_values'][tag_key_id[0]][tag_key_id[2]][tag_key_id[1]] = row.loc[f"CBA {tag_key_id}"]
                elif tag_key_id[0] in cls.cba_input_template['valuestream_values'].keys():
                    cba_dict['valuestream_values'][tag_key_id[0]][tag_key_id[2]][tag_key_id[1]] = row.loc[f"CBA {tag_key_id}"]
                else:
                    cba_dict[tag_key_id[0]][tag_key_id[2]][tag_key_id[1]] = row.loc[f"CBA {tag_key_id}"]
            # flatten dictionaries for VS, Scenario, and Fiances & prepare referenced data
            cba_dict = cls.load_and_prepare_cba(cba_dict, cls.instances[index].Scenario['frequency'])
            cls.instances[index].Finance['CBA'] = cba_dict

    @classmethod
    def load_and_prepare_cba(cls, cba_dict, freq):
        """ Flattens each tag that the Schema has defined to only have 1 allowed. Loads data sets that are specified by the '_filename' parameters

        Returns a params class where the tag attributes that are not allowed to have more than one set of key inputs are just dictionaries of
            their key inputs (while the rest remain dictionaries of the sets of key inputs)
        """
        cba_dict['Scenario'] = cls.flatten_tag_id(cba_dict['Scenario'])
        cba_dict['Finance'] = cls.flatten_tag_id(cba_dict['Finance'])
        cba_dict['valuestream_values']['User'] = cls.flatten_tag_id(cba_dict['valuestream_values']['User'])
        cba_dict['valuestream_values']['Deferral'] = cls.flatten_tag_id(cba_dict['valuestream_values']['Deferral'])

        scenario = cba_dict['Scenario']
        scenario['frequency'] = freq
        if 'time_series_filename' in scenario.keys():
            time_series = cls.referenced_data['time_series'][scenario['time_series_filename']]
            scenario["time_series"] = cls.preprocess_timeseries(time_series, freq)
        if 'monthly_data_filename' in scenario.keys():
            scenario["monthly_data"] = cls.referenced_data["monthly_data"][scenario["monthly_data_filename"]]

        finance = cba_dict['Finance']
        if 'yearly_data_filename' in finance.keys():
            finance["yearly_data"] = cls.referenced_data["yearly_data"][finance["yearly_data_filename"]]
        if 'customer_tariff_filename' in finance.keys():
            finance["customer_tariff"] = cls.referenced_data["customer_tariff"][finance["customer_tariff_filename"]]
        return cba_dict

    def load_finance(self):
        """ Interprets user given data and prepares it for Finance.

        """
        super().load_finance()
        self.Finance.update({'location': self.Scenario['location'],
                             'ownership': self.Scenario['ownership']})

    def load_technology(self):
        """ Interprets user given data and prepares it for each technology.

        """

        time_series = self.Scenario['time_series']
        if len(self.Battery):
            for id_str, battery_inputs in self.Battery.items():
                if not battery_inputs['ch_max_rated'] or not battery_inputs['dis_max_rated']:
                    if not battery_inputs['ch_max_rated']:
                        if battery_inputs['user_ch_rated_min'] > battery_inputs['user_ch_rated_max']:
                            self.record_input_error('Error: User battery min charge power requirement is greater than max charge power requirement.')
                    if not battery_inputs['dis_max_rated']:
                        if battery_inputs['user_dis_rated_min'] > battery_inputs['user_dis_rated_max']:
                            self.record_input_error('User battery min discharge power requirement is greater than max discharge power requirement.')
                if not battery_inputs['ene_max_rated']:
                    if battery_inputs['user_ene_rated_min'] > battery_inputs['user_ene_rated_max']:
                        self.record_input_error('Error: User battery min energy requirement is greater than max energy requirement.')

                # check if user wants to include timeseries constraints -> grab data
                if battery_inputs['incl_ts_energy_limits']:
                    self.load_ts_limits(id_str, battery_inputs, 'Battery', 'Energy', 'kWh', time_series)
                if battery_inputs['incl_ts_charge_limits']:
                    self.load_ts_limits(id_str, battery_inputs, 'Battery', 'Charge', 'kW', time_series)
                if battery_inputs['incl_ts_discharge_limits']:
                    self.load_ts_limits(id_str, battery_inputs, 'Battery', 'Discharge', 'kW', time_series)

        if len(self.CAES):
            for id_str, caes_inputs in self.CAES.items():
                # check if user wants to include timeseries constraints -> grab data
                if caes_inputs['incl_ts_energy_limits']:
                    self.load_ts_limits(id_str, caes_inputs, 'CAES', 'Energy', 'kWh', time_series)
                if caes_inputs['incl_ts_charge_limits']:
                    self.load_ts_limits(id_str, caes_inputs, 'CAES', 'Charge', 'kW', time_series)
                if caes_inputs['incl_ts_discharge_limits']:
                    self.load_ts_limits(id_str, caes_inputs, 'CAES', 'Discharge', 'kW', time_series)

        if len(self.Load):
            if self.Scenario['incl_site_load'] != 1:
                self.record_input_error('Load is active, so incl_site_load should be 1')
            # check to make sure data was included
            for id_str, load_inputs in self.Load.items():
                try:
                    load_inputs['site_load'] = time_series.loc[:, f'Site Load (kW)/{id_str}']
                except KeyError:
                    self.record_input_error(f"Missing 'Site Load (kW)/{id_str}' from timeseries input. Please include a site load.")

                load_inputs.update({'dt': self.Scenario['dt'],
                                    'growth': self.Scenario['def_growth']})
        super().load_technology()

    def load_ts_limits(self, id_str, inputs_dct, tag, measurement, unit, time_series):
        input_cols = [f'{tag}: {measurement} Max ({unit})/{id_str}', f'{tag}: {measurement} Min ({unit})/{id_str}']
        ts_max = time_series.get(input_cols[0])
        ts_min = time_series.get(input_cols[1])
        if ts_max is None and ts_min is None:
            self.record_input_error(f"Missing '{tag}: {measurement} Min ({unit})/{id_str}' or '{tag}: {measurement} Max ({unit})/{id_str}' " +
                                    "from timeseries input. User indicated one needs to be applied. " +
                                    "Please include or turn incl_ts_energy_limits off.")
        if unit == 'kW':
            # preform the following checks on the values in the timeseries
            if ts_max.max() * ts_max.min() < 0:
                # then the max and min are not both positive or both negative -- so error
                self.record_input_error(f"'{tag}: {measurement} Max ({unit})/{id_str}' should be all positive or all negative. " +
                                        "Please fix and rerun.")
            if ts_min.max() * ts_min.min() < 0:
                # then the max and min are not both positive or both negative -- so error
                self.record_input_error(f"'{tag}: {measurement} Min ({unit})/{id_str}' should be all positive or all negative. " +
                                        "Please fix and rerun.")
        if unit == 'kWh':
            # preform the following checks on the values in the timeseries
            if ts_max.max() < 0:
                self.record_input_error(f"'{tag}: {measurement} Max ({unit})/{id_str}' should be greater than 0. Please fix and rerun.")
            if ts_min.max() < 0:
                self.record_input_error(f"'{tag}: {measurement} Min ({unit})/{id_str}' should be greater than 0. Please fix and rerun.")

        inputs_dct.update({f'ts_{measurement.lower()}_max': ts_max,
                           f'ts_{measurement.lower()}_min': ts_min})

    def load_services(self):
        """ Interprets user given data and prepares it for each ValueStream (dispatch and pre-dispatch).

        """
        super().load_services()

        if self.Reliability is not None:
            self.Reliability["dt"] = self.Scenario["dt"]
            try:
                self.Reliability.update({'critical load': self.Scenario['time_series'].loc[:, 'Critical Load (kW)']})
            except KeyError:
                self.record_input_error("Missing 'Critial Load (kW)' from timeseries input. Please include a critical load.")

        # TODO add try statements around each lookup to time_series
        if self.FR is not None:
            if self.FR['u_ts_constraints']:
                self.FR.update({'regu_max': self.Scenario['time_series'].loc[:, 'FR Reg Up Max (kW)'],
                                'regu_min': self.Scenario['time_series'].loc[:, 'FR Reg Up Min (kW)']})
            if self.FR['u_ts_constraints']:
                self.FR.update({'regd_max': self.Scenario['time_series'].loc[:, 'FR Reg Down Max (kW)'],
                                'regd_min': self.Scenario['time_series'].loc[:, 'FR Reg Down Min (kW)']})

        if self.SR is not None:
            if self.SR['ts_constraints']:
                self.SR.update({'max': self.Scenario['time_series'].loc[:, 'SR Max (kW)'],
                                'min': self.Scenario['time_series'].loc[:, 'SR Min (kW)']})

        if self.NSR is not None:
            if self.NSR['ts_constraints']:
                self.NSR.update({'max': self.Scenario['time_series'].loc[:, 'NSR Max (kW)'],
                                 'min': self.Scenario['time_series'].loc[:, 'NSR Min (kW)']})

        if self.LF is not None:
            if self.LF['u_ts_constraints']:
                self.LF.update({'lf_u_max': self.Scenario['time_series'].loc[:, 'LF Reg Up Max (kW)'],
                                'lf_u_min': self.Scenario['time_series'].loc[:, 'LF Reg Up Min (kW)']})
            if self.LF['u_ts_constraints']:
                self.LF.update({'lf_d_max': self.Scenario['time_series'].loc[:, 'LF Reg Down Max (kW)'],
                                'lf_d_min': self.Scenario['time_series'].loc[:, 'LF Reg Down Min (kW)']})

        TellUser.debug("Successfully prepared the value-streams")
