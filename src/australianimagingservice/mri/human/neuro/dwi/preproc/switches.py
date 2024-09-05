import pydra.mark
import attrs
from fileformats.medimage_mrtrix3 import ImageFormat as Mif


@pydra.mark.task
def requires_regrid_switch(
    in_image: Mif, se_epi: Mif
) -> bool:
    if se_epi is not attrs.NOTHING:
        dims_match = (
            in_image.dims()[:3] == se_epi.dims()[:3]
            and in_image.vox_sizes()[:3] == se_epi.vox_sizes()[:3]
        )
    else:
        dims_match = False
    return not dims_match


@pydra.mark.task
def field_estimation_data_formation_strategy_switch(
    in_image: Mif, se_epi: Mif
) -> str:
    pass


#####################
# Unprocessed below #
#####################


# Deal with the phase-encoding of the images to be fed to topup (if applicable)
#execute_topup = (not pe_design == "None") and not topup_file_userpath
overwrite_se_epi_pe_scheme = False
se_epi_path = wf.import_seepi.lzout.output
dwi_permvols_preeddy = None
dwi_permvols_posteddy = None  # inverse of previous
dwi_permvols_posteddy_slice = None
volume_pairs = None  # See examine_metadata:determine_volume_pairs
dwi_bzero_added_to_se_epi = False
se_epi_path = new_se_epi_path
se_epi_header = image.Header(se_epi_path)

# 3 possible sources of PE information: DWI header, topup image header, command-line
# Any pair of these may conflict, and any one could be absent

# Have to switch here based on phase-encoding acquisition design
if pe_design == "Pair":
    # Criteria:
    #   * If present in own header, ignore DWI header entirely -
    #     - If also provided at command-line, look for conflict & report
    #     - If not provided at command-line, nothing to do
    #   * If _not_ present in own header:
    #     - If provided at command-line, infer appropriately
    #     - If not provided at command-line, but the DWI header has that information, infer appropriately
    if se_epi_pe_scheme:
        if manual_pe_dir:
            if not scheme_dirs_match(se_epi_pe_scheme, se_epi_manual_pe_scheme):
                logger.warning(
                    "User-defined phase-encoding direction design does not match what is stored in SE EPI image header; proceeding with user specification"
                )
                overwrite_se_epi_pe_scheme = True
        if manual_trt:
            if not scheme_times_match(
                se_epi_pe_scheme, se_epi_manual_pe_scheme
            ):
                logger.warning(
                    "User-defined total readout time does not match what is stored in SE EPI image header; proceeding with user specification"
                )
                overwrite_se_epi_pe_scheme = True
        if overwrite_se_epi_pe_scheme:
            se_epi_pe_scheme = se_epi_manual_pe_scheme
        else:
            se_epi_manual_pe_scheme = (
                None  # To guarantee that these data are never used
            )
    else:
        overwrite_se_epi_pe_scheme = True
        se_epi_pe_scheme = se_epi_manual_pe_scheme

elif pe_design == "All":
    # Criteria:
    #   * If present in own header:
    #     - Nothing to do
    #   * If _not_ present in own header:
    #     - Don't have enough information to proceed
    #     - Is this too harsh? (e.g. Have rules by which it may be inferred from the DWI header / command-line)
    if not se_epi_pe_scheme:
        raise RuntimeError(
            "If explicitly including SE EPI images when using -rpe_all option, they must come with their own associated phase-encoding information in the image header"
        )

elif pe_design == "Header":
    # Criteria:
    #   * If present in own header:
    #       Nothing to do (-pe_dir option is mutually exclusive)
    #   * If _not_ present in own header:
    #       Cannot proceed
    if not se_epi_pe_scheme:
        raise RuntimeError(
            "No phase-encoding information present in SE-EPI image header"
        )
    # If there is no phase encoding contrast within the SE-EPI series,
    #   try combining it with the DWI b=0 volumes, see if that produces some contrast
    # However, this should probably only be permitted if the -align_seepi option is defined
    se_epi_pe_scheme_has_contrast = "pe_scheme" in se_epi_header.keyval()
    if not se_epi_pe_scheme_has_contrast:
        if align_seepi:
            logger.info(
                "No phase-encoding contrast present in SE-EPI images; will examine again after combining with DWI b=0 images"
            )
            new_se_epi_path = (
                os.path.splitext(se_epi_path)[0] + "_dwibzeros.mif"
            )
            # Don't worry about trying to produce a balanced scheme here
            wf.add(
                dwiextract(
                    input=wf.import_dwi.lzout.output,
                    bzero=True,
                    name="dwi_bzeros_for_align_seepi",
                )
            )
            wf.add(
                mrcat(
                    input=(
                        wf.dwi_bzeros_for_align_seepi.lzout.output,
                        se_epi_path,
                    ),
                    output=new_se_epi_path,
                    axis=3,
                    name="cat_dwi_bzeros_seepi_for_align_seepi",
                )
            )
            se_epi_header = image.Header(new_se_epi_path)
            se_epi_pe_scheme_has_contrast = (
                "pe_scheme" in se_epi_header.keyval()
            )
            if se_epi_pe_scheme_has_contrast:
                app.cleanup(se_epi_path)
                se_epi_path = new_se_epi_path
                se_epi_pe_scheme = phaseencoding.get_scheme(se_epi_header)
                dwi_bzero_added_to_se_epi = True
                # Delay testing appropriateness of the concatenation of these images
                #   (i.e. differences in contrast) to later
            else:
                raise RuntimeError(
                    "No phase-encoding contrast present in SE-EPI images, even after concatenating with b=0 images due to -align_seepi option; "
                    "cannot perform inhomogeneity field estimation"
                )
        else:
            raise RuntimeError(
                "No phase-encoding contrast present in SE-EPI images; cannot perform inhomogeneity field estimation"
            )


@pydra.mark.task
@pydra.mark.annotate()
def recombination_pattern():
    # Get the axis strides from the input series, so the output image can be modified to match
    stride_option = ",".join([str(i) for i in dwi_header.strides()])

    # Determine whether or not volume recombination should be performed
    # This could be either due to use of -rpe_all option, or just due to the data provided with -rpe_header
    # Rather than trying to re-use the code that was used in the case of -rpe_all, run fresh code
    # The phase-encoding scheme needs to be checked also
    volume_matchings = [dwi_num_volumes] * dwi_num_volumes
    volume_pairs = []
    logger.debug(
        "Commencing gradient direction matching; " + str(dwi_num_volumes) + " volumes"
    )
    for index1 in range(dwi_num_volumes):
        if volume_matchings[index1] == dwi_num_volumes:  # As yet unpaired
            for index2 in range(index1 + 1, dwi_num_volumes):
                if volume_matchings[index2] == dwi_num_volumes:  # Also as yet unpaired
                    # Here, need to check both gradient matching and reversed phase-encode direction
                    if not any(
                        dwi_pe_scheme[index1][i] + dwi_pe_scheme[index2][i]
                        for i in range(0, 3)
                    ) and grads_match(index1, index2):
                        volume_matchings[index1] = index2
                        volume_matchings[index2] = index1
                        volume_pairs.append([index1, index2])
                        logger.debug(
                            "Matched volume "
                            + str(index1)
                            + " with "
                            + str(index2)
                            + "\n"
                            + "Phase encoding: "
                            + str(dwi_pe_scheme[index1])
                            + " "
                            + str(dwi_pe_scheme[index2])
                            + "\n"
                            + "Gradients: "
                            + str(grad[index1])
                            + " "
                            + str(grad[index2])
                        )
                        break





    # # If there was any relevant padding applied, then we want to provide
    # #   the comprehensive set of files to EddyQC with that padding removed
    # if dwi_post_eddy_crop:
    #     progress = app.ProgressBar(
    #         "Removing image padding prior to running EddyQC",
    #         len(eddy_suppl_files) + 3,
    #     )

    #     for eddy_filename in eddy_suppl_files:
    #         if os.path.isfile("dwi_post_eddy." + eddy_filename):
    #             if slice_padded and eddy_filename in [
    #                 "eddy_outlier_map",
    #                 "eddy_outlier_n_sqr_stdev_map",
    #                 "eddy_outlier_n_stdev_map",
    #             ]:
    #                 with open(
    #                     "dwi_post_eddy." + eddy_filename, "r", encoding="utf-8"
    #                 ) as f_eddyfile:
    #                     eddy_data = f_eddyfile.readlines()
    #                 eddy_data_header = eddy_data[0]
    #                 eddy_data = eddy_data[1:]
    #                 for line in eddy_data:
    #                     line = " ".join(line.strip().split(" ")[:-1])
    #                 with open(
    #                     "dwi_post_eddy_unpad." + eddy_filename,
    #                     "w",
    #                     encoding="utf-8",
    #                 ) as f_eddyfile:
    #                     f_eddyfile.write(eddy_data_header + "\n")
    #                     f_eddyfile.write("\n".join(eddy_data) + "\n")
    #             elif eddy_filename.endswith(".nii.gz"):
    #                 wf.add(
    #                     mrconvert(
    #                         input="dwi_post_eddy." + eddy_filename,
    #                         coord=dwi_post_eddy_crop,
    #                         name="remove_dwi_padding_for_eddyquad",
    #                     )
    #                 )
    #             else:
    #                 run.function(
    #                     os.symlink,
    #                     "dwi_post_eddy." + eddy_filename,
    #                     "dwi_post_eddy_unpad." + eddy_filename,
    #                 )
    #             app.cleanup("dwi_post_eddy." + eddy_filename)
    #         progress.increment()

    #     if eddy_mporder and slice_padded:
    #         logger.debug("Current slice groups: " + str(slice_groups))
    #         logger.debug(
    #             "Slice encoding direction: " + str(slice_encoding_direction)
    #         )
    #         # Remove padded slice from slice_groups, write new slspec
    #         if sum(slice_encoding_direction) < 0:
    #             slice_groups = [
    #                 [index - 1 for index in group if index]
    #                 for group in slice_groups
    #             ]
    #         else:
    #             slice_groups = [
    #                 [index for index in group if index != dwi_num_slices - 1]
    #                 for group in slice_groups
    #             ]
    #         eddyqc_slspec = "slspec_unpad.txt"
    #         logger.debug("Slice groups after removal: " + str(slice_groups))
    #         try:
    #             # After this removal, slspec should now be a square matrix
    #             assert all(
    #                 len(group) == len(slice_groups[0])
    #                 for group in slice_groups[1:]
    #             )
    #             matrix.save_matrix(
    #                 eddyqc_slspec,
    #                 slice_groups,
    #                 add_to_command_history=False,
    #                 fmt="%d",
    #             )
    #         except AssertionError:
    #             matrix.save_numeric(
    #                 eddyqc_slspec,
    #                 slice_groups,
    #                 add_to_command_history=False,
    #                 fmt="%d",
    #             )
    #             raise

    #     wf.add(
    #         mrconvert(
    #             input="eddy_mask.nii",
    #             output="eddy_mask_unpad.nii",
    #             coord=dwi_post_eddy_crop,
    #             name="brainmask_remove_padding_for_eddyquad",
    #         )
    #     )
    #     eddyqc_mask = "eddy_mask_unpad.nii"
    #     progress.increment()
    #     wf.add(
    #         mrconvert(
    #             input=fsl.find_image("field_map"),
    #             output="field_map_unpad.nii",
    #             coord=dwi_post_eddy_crop,
    #             name="fieldmap_remove_padding_for_eddyquad",
    #         )
    #     )
    #     eddyqc_fieldmap = "field_map_unpad.nii"
    #     progress.increment()
    #     wf.add(
    #         mrconvert(
    #             input=eddy_output_image_path,
    #             output="dwi_post_eddy_unpad.nii.gz",
    #             coord=dwi_post_eddy_crop,
    #             name="dwi_remove_padding_for_eddyquad",
    #         )
    #     )
    #     eddyqc_prefix = "dwi_post_eddy_unpad"
    #     progress.done()

    # if len(volume_pairs) != int(dwi_num_volumes / 2):
    #     if execute_topup:
    #         app.cleanup("topup_in.nii")
    #         app.cleanup(fsl.find_image("field_map"))

    #     # Convert the resulting volume to the output image, and re-insert the diffusion encoding
    #     wf.add(
    #         mrconvert(
    #             input=eddy_output_image_path,
    #             output="result.mif",
    #             coord=(3, 1, dwi_permvols_posteddy_slice),
    #             fslgrad=(bvecs_path, "bvals"),
    #             name="post_eddy_conversion",
    #         )
    #     )  # coord=dwi_post_eddy_crop
    #     app.cleanup(eddy_output_image_path)

    # else:                    